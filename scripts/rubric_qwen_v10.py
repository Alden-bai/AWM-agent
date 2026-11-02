"""Fresh single-task rubric generation; ten frozen calls, 128k guard, no retries."""
import argparse
import asyncio
import json
import re

import rubric_pilot as pilot
import rubric_qwen_pilot as runner
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config
from rubric_pilot import file_hash, read_json, write_json

RUN = pilot.ROOT / 'runs/rubric_qwen_v10_10_20261024'
PROMPT = pilot.ROOT / 'configs/rubric/generate_qwen_v10.txt'
SOURCE = pilot.ROOT / 'runs/rubric_qwen_v8_10_20261024'
DECISIONS = {'satisfied_when': '本条G成立且本条P成立',
             'violated_when': '本条G成立且本条P不成立',
             'unknown_when': '本条G不成立'}


def validate_contract(out, inp):
    errors = pilot.validate_output(out, inp)
    if not isinstance(out, dict):
        return errors
    if set(out) != {'task_id', 'rubric', 'coverage', 'unresolved'}:
        errors.append('wrong_root_keys')
    items = out.get('rubric')
    if not isinstance(items, list):
        return errors
    for r in items:
        if not isinstance(r, dict):
            continue
        rid = r.get('id')
        if set(r) != {'id', 'criterion', 'sources', 'requirement_level', 'evidence', 'decision'}:
            errors.append(f'wrong_item_keys:{rid}')
        criterion = r.get('criterion', '')
        if not isinstance(criterion, str) or not re.fullmatch(r'要求：\S[^\n]*\nG：\S[^\n]*\nP：\S[\s\S]*', criterion):
            errors.append(f'missing_gate_predicate:{rid}')
        decision = r.get('decision')
        if not isinstance(decision, dict) or any(decision.get(k) != v for k, v in DECISIONS.items()):
            errors.append(f'noncanonical_decision:{rid}')
        if inp.get('quality_policy') is None and r.get('requirement_level') != 'required':
            errors.append(f'unsupported_quality:{rid}')
    for entry in out.get('coverage', []) if isinstance(out.get('coverage'), list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get('interpretation'), str) or not entry['interpretation'].strip() or not entry.get('item_ids'):
            errors.append('invalid_coverage_content')
    for entry in out.get('unresolved', []) if isinstance(out.get('unresolved'), list) else []:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in ('issue', 'needed')) or not entry.get('affected_items'):
            errors.append('invalid_unresolved_content')
    return errors


def prepare():
    if (RUN / 'manifest.json').exists():
        raise SystemExit('Already prepared; frozen requests retained')
    cfg = load_qwen_config().actor_api.model_copy(update={'timeout': 240, 'temperature': 0, 'enable_thinking': False})
    assert cfg.model == 'qwen3.5-flash' and cfg.enable_thinking is False
    system = PROMPT.read_text()
    tasks = []
    for t in read_json(SOURCE / 'manifest.json')['tasks']:
        inp = read_json(t['input_ref'])
        assert file_hash(t['input_ref']) == t['input_sha256']
        request = dict(model=cfg.model, temperature=0, max_tokens=8000,
            response_format={'type': 'json_object'}, extra_body={'enable_thinking': False},
            messages=[{'role': 'system', 'content': system},
                      {'role': 'user', 'content': json.dumps(inp, ensure_ascii=False, separators=(',', ':'))}])
        upper = estimate_tokens(request['messages']) + request['max_tokens'] + 512
        if upper >= min(cfg.context_limit, 128000):
            raise ValueError('per_request_context_cap_exceeded')
        dest = RUN / 'initial' / t['task_id'].replace(':', '__') / 'planned_request.json'
        write_json(dest, request)
        tasks.append(dict(task_id=t['task_id'], input_ref=t['input_ref'], input_sha256=t['input_sha256'],
            request_sha256=file_hash(dest), reservation_upper=upper,
            signature=runner.identity(request, cfg.model_dump(), t['input_sha256'])))
    before = runner.ledger_snapshot()
    m = dict(tasks=tasks, api=cfg.model_dump(), model=cfg.model, budget_before=before,
        prompt_sha256=file_hash(PROMPT), source_run=str(SOURCE),
        request_prefix='qwen_rubric_v10', full_run=False, max_requests=10,
        automatic_retries=0, repair_requests=0, per_request_context_cap=128000,
        experiment_cap=200000, cumulative_cap=before['tokens_accounted_or_reserved'] + 200000,
        cap_provenance='operator-imposed additional ceiling within user-authorized ten-task pilot; not a user-quoted token budget',
        review_reference_sha256=file_hash(runner.LEDGER.parent / 'review_reference.json'),
        changes=['fresh generation without prior drafts/feedback', 'full original sources unchanged',
                 'single G/P definition and fixed three-way decisions', 'JSON response mode', '8000 output cap', 'complete worked JSON example and strict coverage requirements'],
        comparison_boundary='same known ten tasks; prompt and input strategy changed together, not a blind ablation',
        price=dict(currency='CNY', input_per_million=0.2, output_per_million=2.0,
                   source='https://help.aliyun.com/zh/model-studio/qwen3-5-flash', checked_date='2026-10-24',
                   tier='Beijing ordinary API, input <=128k', actual_bill_verified=False))
    write_json(RUN / 'manifest.json', m)
    (RUN / 'initial/prompt.txt').write_text(system)
    write_json(RUN / 'authorization.json', dict(source='direct_user_message',
        text='帮我把这个项目的这个prompt改改，单次prompt不能超过128k，这样价格就比较低，帮我试跑10道题',
        approved_scope='Ten tasks, one request each, Qwen3.5-Flash, per-request input <128k',
        experiment_cap=200000, cap_provenance=m['cap_provenance'], manifest_sha256=file_hash(RUN / 'manifest.json')))
    print(json.dumps(dict(prepared=len(tasks), requests_sent=0, budget_before=before,
        operator_experiment_cap=200000, max_request_upper=max(t['reservation_upper'] for t in tasks))))


async def run():
    import sqlite3
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    cfg = load_qwen_config().actor_api.model_copy(update={
        'timeout': 240, 'temperature': 0, 'enable_thinking': False})
    assert cfg.model_dump() == m['api'], 'Configuration changed since preparation'
    budget = pilot.Budget(runner.LEDGER, m['budget_before']['physical_requests'] + 10,
                          m['cumulative_cap'])
    for t in sorted(m['tasks'], key=lambda x: x['reservation_upper'], reverse=True):
        folder = RUN / 'initial' / t['task_id'].replace(':', '__')
        req = read_json(folder / 'planned_request.json')
        assert file_hash(folder / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert runner.identity(req, cfg.model_dump(), t['input_sha256']) == t['signature']
        assert estimate_tokens(req['messages']) + req['max_tokens'] + 512 < 128000
        key = m['request_prefix'] + '/' + t['task_id'] + '/' + t['signature']
        if (folder / 'response.json').exists():
            assert read_json(folder / 'request_identity.json')['signature'] == t['signature']
            continue
        with sqlite3.connect(runner.LEDGER) as db:
            assert not db.execute('select 1 from requests where request_key=?', (key,)).fetchone(), 'Unknown request must not be retried'
        write_json(folder / 'request_identity.json', dict(signature=t['signature'], request_key=key))
        try:
            data = await pilot.api_request(cfg, req, budget, key, m['request_prefix'], folder)
        except Exception as exc:
            # No traceback/SDK headers in terminal output; sanitized evidence is saved by transport.
            raise SystemExit('Request stopped: ' + type(exc).__name__) from None
        print(json.dumps(dict(task_id=t['task_id'], finish=data['choices'][0]['finish_reason'],
                              usage=data.get('usage')), ensure_ascii=False), flush=True)
    print(json.dumps(budget.summary()), flush=True)


def validate():
    runner.previous.RUN = RUN
    runner.previous.validation('initial')
    rows = []
    for t in read_json(RUN / 'manifest.json')['tasks']:
        folder = RUN / 'initial' / t['task_id'].replace(':', '__')
        if (folder / 'rubric.json').exists():
            errors = validate_contract(read_json(folder / 'rubric.json'), read_json(t['input_ref']))
        else:
            errors = ['no_complete_generation']
        rows.append(dict(task_id=t['task_id'], errors=errors))
    write_json(RUN / 'initial/prompt_contract_validation.json', rows)
    render(rows)


def render(rows):
    """Render even structurally invalid model JSON without altering the artifact."""
    errors = {r['task_id']: r['errors'] for r in rows}
    lines = ['# Qwen v10：10题原始rubric阅读版', '',
             '模型原始内容，仅整理JSON缩进。格式检查和语义审查不同；无人工补写、无旧稿替换。', '']
    for t in read_json(RUN / 'manifest.json')['tasks']:
        folder = RUN / 'initial' / t['task_id'].replace(':', '__')
        lines += ['## '+t['task_id'], '', read_json(t['input_ref'])['task'], '',
                  '五字段及新prompt约定检查：'+('通过' if not errors[t['task_id']] else '；'.join(errors[t['task_id']])), '',
                  f'[API原始返回]({folder}/response.json)', '']
        if (folder / 'response.json').exists():
            raw = read_json(folder / 'response.json')['choices'][0]['message'].get('content') or ''
            try:
                raw = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
            except ValueError:
                pass
            lines += ['```json', raw, '```', '']
        else:
            lines += ['未收到模型返回。', '']
    (RUN / 'initial/rubrics_zh.md').write_text('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'run', 'validate'])
    action = parser.parse_args().action
    if action == 'prepare':
        prepare()
    elif action == 'run':
        asyncio.run(run())
        validate()
    else:
        validate()
