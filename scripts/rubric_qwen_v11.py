"""Ten-task normalized-input -> obligation plan -> rubric -> semantic pilot.

Three calls per task, no retry/repair. Original sources, prompts, requests,
dependency hashes, raw answers and usage are immutable audit artifacts.
"""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

import rubric_qwen_v10 as baseline
from rubric_qwen_pilot import LEDGER, identity, ledger_snapshot
from rubric_pilot import Budget, api_request, file_hash, read_json, write_json
from rubric_workflow import normalize, generator_input, validate_plan, validate_rubric, score_review
from harness_awm.protocol import estimate_tokens, unique_pairs
from harness_awm.qwen import load_qwen_config

ROOT = baseline.pilot.ROOT
RUN = ROOT / 'runs/rubric_qwen_v11_10_20261024'
BASE = ROOT / 'runs/rubric_qwen_v10_thinking_10_20261024'
PROMPTS = ROOT / 'configs/rubric'
STAGES = ('extract', 'generate', 'review')
THINKING = 16384  # Same as the immediately preceding ten-task baseline.
OUTPUT = dict(extract=8000, generate=8000, review=8000)
CAP = 600000  # Operator ceiling for this explicitly authorized ten-task workflow.


def config():
    return load_qwen_config().actor_api.model_copy(update={'temperature':0,'timeout':600,'enable_thinking':True})


class WorkflowBudget(Budget):
    def reserve(self, key, stage, messages, output_tokens):
        return super().reserve(key, stage, messages, output_tokens + THINKING)


def reservation(req):
    return estimate_tokens(req['messages']) + req['max_tokens'] + req['extra_body']['thinking_budget'] + 512


def freeze(path, value):
    if path.exists():
        if read_json(path) != value:
            raise ValueError('frozen_artifact_changed:' + str(path))
    else:
        write_json(path, value)


def prepare():
    if (RUN/'manifest.json').exists():
        raise SystemExit('Already prepared; frozen artifacts retained')
    old = read_json(BASE/'manifest.json')
    cfg = config()
    assert cfg.model == old['model'] == 'qwen3.5-flash'
    prompts = dict(extract=(PROMPTS/'extract_v11.txt').read_text(),
                   generate=baseline.PROMPT.read_text()+'\n'+(PROMPTS/'workflow_v11.txt').read_text(),
                   review=(PROMPTS/'review_v11.txt').read_text())
    for name, prompt in prompts.items():
        path = RUN/'prompts'/f'{name}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt)
    tasks = []
    for t in old['tasks']:
        assert file_hash(t['input_ref']) == t['input_sha256']
        inp = read_json(t['input_ref'])
        normalized, audit = normalize(inp)
        folder = RUN/'inputs'/t['task_id'].replace(':','__')
        write_json(folder/'original.json', inp)
        write_json(folder/'normalized.json', normalized)
        write_json(folder/'normalization_audit.json', audit)
        tasks.append(dict(task_id=t['task_id'], input_ref=str(folder/'original.json'),
                          input_sha256=file_hash(folder/'original.json'),
                          original_ref=t['input_ref'], original_sha256=t['input_sha256'],
                          normalized_ref=str(folder/'normalized.json'), normalized_sha256=file_hash(folder/'normalized.json')))
    before = ledger_snapshot()
    m = dict(version='rubric-workflow-v11', tasks=tasks, api=cfg.model_dump(), model=cfg.model,
             baseline_run=str(BASE), baseline_manifest_sha256=file_hash(BASE/'manifest.json'),
             baseline_cost_report_sha256=file_hash(BASE/'cost_report.json'),
             budget_before=before, experiment_cap=CAP, cumulative_cap=before['tokens_accounted_or_reserved']+CAP,
             cap_provenance='operator ceiling for authorized ten-task workflow, not a user-quoted token budget',
             request_prefix='qwen_rubric_v11', max_requests=30, calls_per_task=3,
             automatic_retries=0, paid_repairs=0, full_run=False, concurrency=2,
             per_request_context_guard=128000, thinking_budget=THINKING, max_answer_tokens=OUTPUT,
             prompt_hashes={k:file_hash(RUN/'prompts'/f'{k}.txt') for k in STAGES},
             implementation_hashes={str(p):file_hash(p) for p in [Path(__file__), ROOT/'scripts/rubric_workflow.py']},
             review_reference_sha256=file_hash(LEDGER.parent/'review_reference.json'),
             price_reference=read_json(BASE/'cost_report.json')['price'],
             comparison_boundary='Known ten-task development set; three workflow changes bundled; not a blind ablation or runtime evaluation',
             quality_gate=dict(all_10_structural=True, independent_source_review_required=True,
                 model_semantic_checks_not_ground_truth=True, invalid_or_missing_checks_never_pass=True,
                 cases_withheld_from_generation=True, historical_review_reference_withheld_from_all_model_calls=True))
    write_json(RUN/'manifest.json', m)
    write_json(RUN/'authorization.json', dict(source='direct_user_message',
        text='那你先帮我把输入规范化、义务清单和语义正反例检查这三部分落地，保持现在的五字段模板，再用这10道题试跑一下，看看效果有没有提升。',
        approved_scope='Ten tasks through three-stage workflow; no repairs or full run',
        manifest_sha256=file_hash(RUN/'manifest.json')))
    print(json.dumps(dict(prepared=len(tasks), max_calls=30, operator_token_cap=CAP, budget_before=before), ensure_ascii=False))


def parse_response(data):
    choice = data['choices'][0]
    if choice['finish_reason'] != 'stop':
        raise ValueError('incomplete_generation')
    return json.loads(choice['message'].get('content') or '', object_pairs_hook=unique_pairs)


async def run():
    m = read_json(RUN/'manifest.json')
    assert read_json(RUN/'authorization.json')['manifest_sha256'] == file_hash(RUN/'manifest.json')
    cfg = config()
    assert cfg.model_dump() == m['api']
    for p, h in m['implementation_hashes'].items():
        assert file_hash(p) == h, 'Implementation changed after preparation'
    for s, h in m['prompt_hashes'].items():
        assert file_hash(RUN/'prompts'/f'{s}.txt') == h
    budget = WorkflowBudget(LEDGER, m['budget_before']['physical_requests']+m['max_requests'], m['cumulative_cap'])
    semaphore = asyncio.Semaphore(m['concurrency'])

    async def call(stage, t, payload, dependencies):
        d = RUN/stage/t['task_id'].replace(':','__')
        req = dict(model=cfg.model, temperature=0, max_tokens=OUTPUT[stage],
                   response_format={'type':'json_object'},
                   extra_body={'enable_thinking':True, 'thinking_budget':THINKING},
                   messages=[{'role':'system','content':(RUN/'prompts'/f'{stage}.txt').read_text()},
                             {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}])
        upper = reservation(req)
        if upper >= min(128000, cfg.context_limit):
            raise ValueError('context_guard_exceeded')
        freeze(d/'planned_request.json', req)
        sig = identity(req, cfg.model_dump(), t['input_sha256'])
        key = m['request_prefix']+'/'+stage+'/'+t['task_id']+'/'+sig
        freeze(d/'request_identity.json', dict(signature=sig, request_key=key, request_sha256=file_hash(d/'planned_request.json'),
            reservation_upper=upper, dependencies=dependencies))
        if (d/'response.json').exists():
            data = read_json(d/'response.json')
        else:
            with sqlite3.connect(LEDGER) as db:
                if db.execute('select 1 from requests where request_key=?',(key,)).fetchone():
                    raise ValueError('unknown_or_inflight_request_not_retried')
            async with semaphore:
                data = await api_request(cfg, req, budget, key, 'v11_'+stage, d)
            print(json.dumps(dict(task_id=t['task_id'],stage=stage,finish=data['choices'][0]['finish_reason'],usage=data.get('usage')),ensure_ascii=False),flush=True)
        (d/'raw_content.txt').write_text(data['choices'][0]['message'].get('content') or '')
        out = parse_response(data)
        freeze(d/'output.json', out)
        return out

    async def task(t):
        name = t['task_id'].replace(':','__')
        state = dict(task_id=t['task_id'], stage='extract', status='running')
        try:
            assert file_hash(t['input_ref']) == t['input_sha256']
            assert file_hash(t['normalized_ref']) == t['normalized_sha256']
            inp, norm = read_json(t['input_ref']), read_json(t['normalized_ref'])
            plan = await call('extract',t,norm,dict(normalized=t['normalized_sha256']))
            errors = validate_plan(plan,inp)
            write_json(RUN/'extract'/name/'validation.json',dict(errors=errors))
            if errors:
                state.update(status='invalid_plan', errors=errors)
                return
            state['stage']='generate'
            generated = await call('generate',t,generator_input(norm,plan),dict(plan=file_hash(RUN/'extract'/name/'output.json')))
            errors = validate_rubric(generated,inp,plan)
            write_json(RUN/'generate'/name/'validation.json',dict(errors=errors))
            # A parseable but structurally flawed rubric is still reviewed; never repaired silently.
            state['stage']='review'
            review_payload = dict(norm, obligation_plan={k:plan[k] for k in ('obligations','ambiguities')},
                                  cases=plan['cases'], candidate_rubric=generated)
            review = await call('review',t,review_payload,dict(plan=file_hash(RUN/'extract'/name/'output.json'),
                rubric=file_hash(RUN/'generate'/name/'output.json')))
            score = score_review(review,plan,generated)
            write_json(RUN/'review'/name/'validation.json',score)
            state.update(status='completed',rubric_errors=errors,semantic_candidate=score['semantic_candidate'],review_errors=score['errors'])
        except Exception as exc:
            # Avoid SDK traceback/headers. Transport keeps sanitized failure evidence.
            state.update(status='stopped', error_type=type(exc).__name__)
        finally:
            write_json(RUN/'task_status'/f'{name}.json',state)
            print(json.dumps(state,ensure_ascii=False),flush=True)

    await asyncio.gather(*(task(t) for t in m['tasks']))
    print(json.dumps(budget.summary()),flush=True)
    summarize()


def summarize():
    m = read_json(RUN/'manifest.json')
    rows, usage_rows, reading = [], [], ['# v11：10题五字段rubric原始输出','',
        '原始模型产物，未人工补写；语义案例是模型模拟，不是实际任务执行。','']
    with sqlite3.connect(f'file:{LEDGER}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        ledger=[dict(r) for r in db.execute('select * from requests')]
    own=[r for r in ledger if r['request_key'].startswith(m['request_prefix']+'/')]
    for t in m['tasks']:
        name=t['task_id'].replace(':','__')
        r=dict(task_id=t['task_id'], rubric_errors=['not_generated'], semantic_candidate=False)
        for stage in STAGES:
            d=RUN/stage/name
            if (d/'response.json').exists():
                data=read_json(d/'response.json'); usage=data.get('usage') or {}
                rid=read_json(d/'request_identity.json'); req=read_json(d/'planned_request.json')
                assert file_hash(d/'planned_request.json')==rid['request_sha256']
                assert req==read_json(d/'request.json')
                assert identity(req,m['api'],t['input_sha256'])==rid['signature']
                matched=[x for x in own if x['request_key']==rid['request_key']]
                assert len(matched)==1 and matched[0]['status']=='response'
                assert json.loads(matched[0]['usage'])==usage
                assert usage['prompt_tokens']+usage['completion_tokens']==usage['total_tokens']==matched[0]['reserved']
                assert usage['total_tokens'] <= rid['reservation_upper'] < 128000
                usage_rows.append(dict(task_id=t['task_id'],stage=stage,**{k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')},
                    reasoning_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0),
                    reservation_upper=rid['reservation_upper']))
        g=RUN/'generate'/name
        p=RUN/'extract'/name/'output.json'
        if (g/'output.json').exists() and p.exists():
            r['rubric_errors']=validate_rubric(read_json(g/'output.json'),read_json(t['input_ref']),read_json(p))
            reading += ['## '+t['task_id'],'',read_json(t['input_ref'])['task'],'','```json',
                        json.dumps(read_json(g/'output.json'),ensure_ascii=False,indent=2),'```','']
        rev=RUN/'review'/name/'output.json'
        if rev.exists() and (g/'output.json').exists():
            score=score_review(read_json(rev),read_json(p),read_json(g/'output.json'))
            r.update(score)
        rows.append(r)
    totals={k:sum(x[k] for x in usage_rows) for k in ('prompt_tokens','completion_tokens','total_tokens','reasoning_tokens')}
    stages={s:{k:sum(x[k] for x in usage_rows if x['stage']==s) for k in totals} for s in STAGES}
    summary=dict(tasks=10, generated=sum((RUN/'generate'/t['task_id'].replace(':','__')/'output.json').exists() for t in m['tasks']),
        strict_contract_pass=sum(not r['rubric_errors'] for r in rows),
        model_semantic_candidates=sum(r['semantic_candidate'] for r in rows),
        model_joint_candidates=sum(not r['rubric_errors'] and r['semantic_candidate'] for r in rows),
        independent_review_complete=(RUN/'source_review.json').exists(), runtime_accuracy_measured=False)
    report=dict(totals=totals,stages=stages,summary=summary,tasks=rows,requests=usage_rows,
        physical_requests=len(own),unknown_reserved=sum(r['reserved'] for r in own if r['status']!='response'),
        cumulative_confirmed=sum(r['reserved'] for r in ledger if r['status']=='response'),
        cumulative_unknown_reserved=sum(r['reserved'] for r in ledger if r['status']!='response'),
        experiment_cap=m['experiment_cap'],actual_bill_verified=False,price_reference=m['price_reference'])
    write_json(RUN/'cost_report.json',report)
    write_json(RUN/'status.json',summary)
    (RUN/'rubrics_zh.md').write_text('\n'.join(reading))
    print(json.dumps(dict(totals=totals,summary=summary),ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','run','summarize'])
    action=parser.parse_args().action
    if action=='prepare': prepare()
    elif action=='run': asyncio.run(run())
    else: summarize()
