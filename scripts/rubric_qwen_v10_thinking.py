"""Paired v10 thinking pilot: immutable messages, ten calls, bounded reasoning."""
import argparse
import asyncio
import copy
import json
import sqlite3

import rubric_qwen_v10 as baseline
from rubric_qwen_pilot import LEDGER, identity, ledger_snapshot
from rubric_pilot import Budget, api_request, file_hash, read_json, write_json
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config

BASE = baseline.RUN
RUN = baseline.pilot.ROOT / 'runs/rubric_qwen_v10_thinking_10_20261024'
PROMPT = baseline.PROMPT
THINKING_BUDGET = 16384
EXPERIMENT_CAP = 300000


def config():
    return load_qwen_config().actor_api.model_copy(update={
        'enable_thinking': True, 'temperature': 0, 'timeout': 600})


def paired_request(old):
    req = copy.deepcopy(old)
    assert req['extra_body'] == {'enable_thinking': False}
    req['extra_body'] = {'enable_thinking': True, 'thinking_budget': THINKING_BUDGET}
    return req


def reservation(req):
    return estimate_tokens(req['messages']) + req['max_tokens'] + req['extra_body']['thinking_budget'] + 512


class ThinkingBudget(Budget):
    """Qwen max_tokens excludes reasoning, so reserve both before dispatch."""
    def reserve(self, key, stage, messages, output_tokens):
        return super().reserve(key, stage, messages, output_tokens + THINKING_BUDGET)


def prepare():
    if (RUN / 'manifest.json').exists():
        raise SystemExit('Already prepared; immutable requests retained')
    old = read_json(BASE / 'manifest.json')
    assert file_hash(PROMPT) == old['prompt_sha256']
    cfg = config()
    assert cfg.model == old['model'] == 'qwen3.5-flash'
    assert cfg.base_url == old['api']['base_url']
    tasks = []
    for t in old['tasks']:
        source = BASE / 'initial' / t['task_id'].replace(':', '__') / 'request.json'
        prior = read_json(source)
        assert prior == read_json(source.parent / 'planned_request.json')
        assert file_hash(source) == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        req = paired_request(prior)
        upper = reservation(req)
        if upper >= min(cfg.context_limit, 128000):
            raise ValueError('context_cap_exceeded_including_reasoning')
        dest = RUN / 'initial' / t['task_id'].replace(':', '__') / 'planned_request.json'
        write_json(dest, req)
        tasks.append(dict(task_id=t['task_id'], input_ref=t['input_ref'], input_sha256=t['input_sha256'],
            request_sha256=file_hash(dest), baseline_request_sha256=file_hash(source),
            reservation_upper=upper, signature=identity(req, cfg.model_dump(), t['input_sha256'])))
    before = ledger_snapshot()
    m = dict(tasks=tasks, api=cfg.model_dump(), model=cfg.model, budget_before=before,
        baseline_run=str(BASE), baseline_manifest_sha256=file_hash(BASE / 'manifest.json'),
        baseline_cost_report_sha256=file_hash(BASE / 'cost_report.json'),
        prompt_sha256=file_hash(PROMPT), validator_sha256=file_hash(baseline.__file__),
        review_reference_sha256=file_hash(LEDGER.parent / 'review_reference.json'),
        request_prefix='qwen_rubric_v10_thinking', max_requests=10, automatic_retries=0,
        paid_repairs=0, full_run=False, experiment_cap=EXPERIMENT_CAP,
        cumulative_cap=before['tokens_accounted_or_reserved']+EXPERIMENT_CAP,
        cap_provenance='operator protection ceiling, not user-specified total budget',
        per_request_input_cap=128000, per_request_context_guard=128000,
        thinking_budget=THINKING_BUDGET, max_answer_tokens=8000,
        messages_identical=True, wire_changes=['extra_body.enable_thinking: false -> true',
            'extra_body.thinking_budget: unset -> 16384'],
        price=old['price'])
    write_json(RUN / 'manifest.json', m)
    (RUN / 'initial/prompt.txt').write_text(PROMPT.read_text())
    write_json(RUN / 'authorization.json', dict(source='direct_user_message',
        text='开启思考模式，用同样的prompt再跑这10道题，单次输入还是不能超过128k，对比token和费用',
        approved_scope='Same ten tasks and prompt, thinking enabled, one call each, input below 128k',
        experiment_cap=EXPERIMENT_CAP, manifest_sha256=file_hash(RUN / 'manifest.json')))
    print(json.dumps(dict(prepared=len(tasks), requests_sent=0, budget_before=before,
                         max_reserved=max(t['reservation_upper'] for t in tasks)), ensure_ascii=False))


async def run(reverse=False):
    m = read_json(RUN / 'manifest.json')
    assert read_json(RUN / 'authorization.json')['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    cfg = config()
    assert cfg.model_dump() == m['api']
    assert file_hash(PROMPT) == m['prompt_sha256']
    budget = ThinkingBudget(LEDGER, m['budget_before']['physical_requests'] + 10, m['cumulative_cap'])
    for t in sorted(m['tasks'], key=lambda x: x['reservation_upper'], reverse=not reverse):
        d = RUN / 'initial' / t['task_id'].replace(':', '__')
        req = read_json(d / 'planned_request.json')
        assert file_hash(d / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert req == paired_request(read_json(BASE / 'initial' / d.name / 'request.json'))
        assert reservation(req) == t['reservation_upper'] < 128000
        assert identity(req, cfg.model_dump(), t['input_sha256']) == t['signature']
        key = m['request_prefix'] + '/' + t['task_id'] + '/' + t['signature']
        if (d / 'response.json').exists():
            assert read_json(d / 'request_identity.json')['signature'] == t['signature']
            continue
        with sqlite3.connect(LEDGER) as db:
            existing = db.execute('select status from requests where request_key=?', (key,)).fetchone()
            if existing:
                if reverse:
                    continue  # Another dispatcher owns this immutable request.
                raise AssertionError('No retry of unknown usage')
        write_json(d / 'request_identity.json', dict(signature=t['signature'], request_key=key))
        try:
            data = await api_request(cfg, req, budget, key, m['request_prefix'], d)
        except sqlite3.IntegrityError:
            if reverse:
                continue  # Atomic reservation lost: no API request was sent.
            raise
        except Exception as exc:
            raise SystemExit('Request stopped: ' + type(exc).__name__) from None
        choice = data['choices'][0]
        print(json.dumps(dict(task_id=t['task_id'], finish=choice['finish_reason'], usage=data.get('usage'),
            reasoning_present=bool(choice['message'].get('reasoning_content'))), ensure_ascii=False), flush=True)
    print(json.dumps(budget.summary()), flush=True)


def validate():
    baseline.RUN = RUN
    baseline.validate()
    p = RUN / 'initial/rubrics_zh.md'
    p.write_text(p.read_text().replace('# Qwen v10：', '# Qwen v10 开启思考：', 1))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['prepare', 'run', 'run-remaining', 'validate'])
    a = p.parse_args().action
    if a == 'prepare': prepare()
    elif a == 'run-remaining':
        asyncio.run(run(reverse=True))
    elif a == 'run':
        asyncio.run(run())
        validate()
    else: validate()
