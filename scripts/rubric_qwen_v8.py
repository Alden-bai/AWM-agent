"""Qwen v8 prompt pilot: full original sources, previous Qwen drafts, scoped feedback."""
import argparse
import asyncio
import json

import rubric_pilot as pilot
import rubric_qwen_pilot as runner
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config
from rubric_pilot import file_hash, read_json, write_json

SOURCE = runner.RUN
RUN = pilot.ROOT / 'runs/rubric_qwen_v8_10_20261024'
PROMPT = pilot.ROOT / 'configs/rubric/generate_qwen_v8.txt'


def prepare():
    if (RUN / 'manifest.json').exists():
        raise SystemExit('Already prepared; preserve frozen requests')
    config = load_qwen_config().actor_api.model_copy(update={'timeout': 240, 'temperature': 0})
    assert config.model == 'qwen3.5-flash' and config.enable_thinking is False
    old = read_json(SOURCE / 'manifest.json')
    review = read_json(SOURCE / 'initial/source_review.json')
    feedback = {t['task_id']: t for t in review['tasks']}
    system = PROMPT.read_text() + '\n' + pilot.SERIALIZATION
    tasks = []
    for t in old['tasks']:
        name = t['task_id'].replace(':', '__')
        prior = read_json(SOURCE / 'initial' / name / 'request.json')
        original = read_json(t['input_ref'])
        payload = json.loads(prior['messages'][1]['content'])
        payload['sources'] = original['sources']
        draft = read_json(SOURCE / 'initial' / name / 'rubric.json')
        payload['previous_rubric_raw'] = json.dumps(draft, ensure_ascii=False, separators=(',', ':'))
        f = feedback[t['task_id']]
        payload['source_review_feedback'] = {k: f[k] for k in ('task_id', 'status', 'findings')}
        payload['repair_scope'] = '修复核实的问题及受其影响的绑定/coverage；没有问题的条目尽量保留。所有quote核对本次提供的完整原始sources；不追加无依据义务。'
        request = dict(prior, messages=[{'role': 'system', 'content': system},
                        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}])
        assert {k: v for k, v in prior.items() if k != 'messages'} == {k: v for k, v in request.items() if k != 'messages'}
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert json.loads(payload['previous_rubric_raw']) == draft
        assert payload['sources'] == original['sources']
        upper = estimate_tokens(request['messages']) + request['max_tokens'] + 512
        assert upper < min(config.context_limit, 128000)
        dest = RUN / 'initial' / name / 'planned_request.json'
        write_json(dest, request)
        usage = read_json(SOURCE / 'initial' / name / 'response.json')['usage']
        # Observed token/byte ratio is an estimate only; actual ledger reserves bytes.
        estimate = round(usage['prompt_tokens'] * estimate_tokens(request['messages']) / estimate_tokens(prior['messages']))
        tasks.append(dict(task_id=t['task_id'], input_ref=t['input_ref'], input_sha256=t['input_sha256'],
            request_sha256=file_hash(dest), signature=runner.identity(request, config.model_dump(), t['input_sha256']),
            reservation_upper=upper, estimated_prompt_tokens=estimate,
            estimated_completion_tokens=usage['completion_tokens'],
            draft_sha256=file_hash(SOURCE / 'initial' / name / 'rubric.json')))
    before = runner.ledger_snapshot()
    assert before == {'physical_requests': 118, 'tokens_accounted_or_reserved': 1001142}
    m = dict(tasks=tasks, api=config.model_dump(), model=config.model, base_url=config.base_url,
        budget_before=before, full_run=False, max_requests=10, automatic_retries=0, repair_requests=0,
        request_prefix='qwen_rubric_v8', source_run=str(SOURCE), prompt_sha256=file_hash(PROMPT),
        per_request_context_cap=128000, experiment_cap=160000, cumulative_cap=before['tokens_accounted_or_reserved']+160000,
        source_review_sha256=file_hash(SOURCE / 'initial/source_review.json'),
        baseline_review_sha256=old['baseline_review_sha256'], review_reference_sha256=old['review_reference_sha256'],
        changes=['v8 system prompt preserving v7 requirements', 'restore full original public sources including examples',
                 'previous Qwen draft plus its audited feedback; draft JSON whitespace only compacted'],
        comparison_boundary='iterative same-sample repair, not isolated prompt ablation or fresh-task generalization',
        estimated_prompt_tokens=sum(t['estimated_prompt_tokens'] for t in tasks),
        estimated_completion_tokens=sum(t['estimated_completion_tokens'] for t in tasks),
        quality_target='>=8/10 provisional source-audited drafts, with original-source checks; no paid repairs after this round',
        user_supplied_price={'currency': 'CNY', 'input_per_million': 0.2, 'output_per_million': 2.0,
                            'source': 'user screenshot, ordinary API tier input <=128k; estimate not verified bill'})
    write_json(RUN / 'manifest.json', m)
    (RUN / 'initial/prompt.txt').write_text(system)
    write_json(RUN / 'authorization.json', dict(source='direct_user_message', text='按单次128k、本轮16万执行',
        approved_scope='Same ten tasks, Qwen3.5-Flash, revised prompt and full original sources; no additional repairs or full run',
        experiment_cap=160000, manifest_sha256=file_hash(RUN / 'manifest.json')))
    print(json.dumps(dict(prepared=10, requests_sent=0,
        estimated_tokens=m['estimated_prompt_tokens']+m['estimated_completion_tokens'],
        max_request_upper=max(t['reservation_upper'] for t in tasks), cumulative_cap=m['cumulative_cap']), ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['prepare', 'run', 'validate', 'render'])
    a = p.parse_args()
    if a.action == 'prepare':
        prepare()
    elif a.action == 'run':
        runner.RUN = RUN
        asyncio.run(runner.run())
    elif a.action == 'validate':
        runner.previous.RUN = RUN
        runner.previous.validation('initial')
    else:
        pilot.RUN = RUN
        pilot.render('initial')
