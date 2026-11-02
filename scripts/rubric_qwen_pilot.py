"""Matched Qwen replay of the frozen v7-input-only ten-task pilot."""
import argparse
import asyncio
import hashlib
import json
import sqlite3

import rubric_pilot as pilot
import rubric_v7_input_only as previous
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config
from rubric_pilot import Budget, api_request, file_hash, read_json, write_json

SOURCE = previous.RUN
RUN = pilot.ROOT / 'runs/rubric_qwen10_20261024'
LEDGER = previous.BASE / 'budget.sqlite'


def ledger_snapshot():
    with sqlite3.connect(f'file:{LEDGER}?mode=ro', uri=True) as db:
        count, total = db.execute('select count(*),sum(reserved) from requests').fetchone()
    return dict(physical_requests=count, tokens_accounted_or_reserved=total)


def identity(request, api, source_hash):
    value = dict(request=request, api=api, source_sha256=source_hash)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare():
    if (RUN / 'manifest.json').exists():
        raise SystemExit('Prepared already; frozen requests retained')
    cfg = load_qwen_config().actor_api.model_copy(update={'timeout': 240, 'temperature': 0})
    assert cfg.model == 'qwen3.5-flash' and cfg.enable_thinking is False
    source = read_json(SOURCE / 'manifest.json')
    tasks = []
    for t in source['tasks']:
        name = t['task_id'].replace(':', '__')
        old = read_json(SOURCE / 'initial' / name / 'request.json')
        request = dict(old, model=cfg.model, extra_body={'enable_thinking': False})
        assert request['messages'] == old['messages']
        assert request['max_tokens'] == 10000
        assert file_hash(t['input_ref']) == t['input_sha256']
        reserve = estimate_tokens(request['messages']) + request['max_tokens'] + 512
        assert reserve <= cfg.context_limit
        dest = RUN / 'initial' / name / 'planned_request.json'
        write_json(dest, request)
        tasks.append(dict(task_id=t['task_id'], input_ref=t['input_ref'], input_sha256=t['input_sha256'],
                          request_sha256=file_hash(dest), reservation_upper=reserve,
                          signature=identity(request, cfg.model_dump(), t['input_sha256'])))
    before = ledger_snapshot()
    write_json(RUN / 'manifest.json', dict(tasks=tasks, api=cfg.model_dump(), model=cfg.model,
        base_url=cfg.base_url, budget_before=before, source_run=str(SOURCE),
        dispatch_status='pending_budget_authorization', full_run=False, max_requests=10,
        automatic_retries=0, repair_requests=0, proposed_experiment_cap=160000,
        proposed_cumulative_cap=before['tokens_accounted_or_reserved']+160000,
        existing_cumulative_cap=954558, existing_remaining=954558-before['tokens_accounted_or_reserved'],
        baseline_review_sha256=file_hash(previous.BASE / 'v7/source_review.json'),
        review_reference_sha256=file_hash(previous.BASE / 'review_reference.json'),
        same_system_and_user_messages=True,
        cost_estimate_note='Prior DeepSeek actual 102952 is a reference only; Qwen tokenization/output may differ.'))
    (RUN / 'initial/prompt.txt').write_text((SOURCE / 'initial/prompt.txt').read_text())
    print(json.dumps(dict(prepared=len(tasks), requests_sent=0, budget_before=before,
                         max_single_reservation=max(t['reservation_upper'] for t in tasks)), ensure_ascii=False))


async def run():
    m = read_json(RUN / 'manifest.json')
    # A direct user-approved cap must be recorded separately; preparation grants none.
    auth = read_json(RUN / 'authorization.json')
    assert auth['source'] == 'direct_user_message' and auth['text'].strip()
    assert auth['experiment_cap'] > 0
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    cfg = load_qwen_config().actor_api.model_copy(update={'timeout': 240, 'temperature': 0})
    assert cfg.model_dump() == m['api'], 'Configuration changed since preparation'
    b = Budget(LEDGER, m['budget_before']['physical_requests'] + 10,
               m['budget_before']['tokens_accounted_or_reserved'] + auth['experiment_cap'])
    for t in sorted(m['tasks'], key=lambda t: t['reservation_upper'], reverse=True):
        d = RUN / 'initial' / t['task_id'].replace(':', '__')
        request = read_json(d / 'planned_request.json')
        assert file_hash(d / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        sig = identity(request, cfg.model_dump(), t['input_sha256'])
        assert sig == t['signature']
        key = m.get('request_prefix', 'qwen_rubric_initial') + '/' + t['task_id'] + '/' + sig
        if (d / 'response.json').exists():
            assert read_json(d / 'request_identity.json')['signature'] == sig
            continue
        with sqlite3.connect(LEDGER) as db:
            assert not db.execute('select 1 from requests where request_key=?', (key,)).fetchone(), 'Unknown request is never automatically retried'
        write_json(d / 'request_identity.json', dict(signature=sig, request_key=key))
        data = await api_request(cfg, request, b, key, m.get('request_prefix', 'rubric_qwen_initial'), d)
        print(json.dumps(dict(task_id=t['task_id'], finish=data['choices'][0]['finish_reason'],
                              usage=data.get('usage')), ensure_ascii=False), flush=True)
    previous.RUN = RUN
    previous.validation('initial')
    print(json.dumps(b.summary()))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['prepare', 'run', 'validate', 'render'])
    a = p.parse_args()
    if a.action == 'prepare':
        prepare()
    elif a.action == 'run':
        asyncio.run(run())
    elif a.action == 'validate':
        previous.RUN = RUN
        previous.validation('initial')
    else:
        pilot.RUN = RUN
        pilot.render('initial')
