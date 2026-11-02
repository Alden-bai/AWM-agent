"""All-Qwen ten-task pilot: fresh judges, no-zero acceptance, <=2 revisions.

Historical v12 code/artifacts remain immutable. Network errors are not retried.
"""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from rubric_pilot import ROOT, Budget, api_request, read_json, write_json, file_hash
from rubric_qwen_pilot import LEDGER, identity, ledger_snapshot
from rubric_qwen_v11 import freeze, parse_response
from rubric_candidates import prepare_input, validate_selection, compile_rubric, evaluate_scores, DIMENSIONS
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config

RUN = ROOT / 'runs/rubric_qwen_v13_10_20261024'
BASE = ROOT / 'runs/rubric_qwen_deepseek_v12_10_20261024'
THINKING = 16384
OUTPUT = 10000
CAP = 1500000
MAX_REVISIONS = 2
PROMPTS = dict(generate=ROOT/'configs/rubric/select_compile_v13.txt', judge=ROOT/'configs/rubric/judge_qwen_v13.txt')


def config():
    api = load_qwen_config().actor_api.model_copy(update=dict(temperature=0, timeout=600, enable_thinking=True, context_limit=128000))
    assert api.model == 'qwen3.5-flash'
    return api


def evaluate(out, task_id, item_ids, candidate_ids):
    result = evaluate_scores(out, task_id, item_ids, candidate_ids)
    result['status'] = 'invalid_judge' if result['errors'] else ('rejected' if result['zero_dimensions'] else 'passed')
    return result


def request(payload, api, prompt):
    return dict(model=api.model, temperature=0, max_tokens=OUTPUT,
        extra_body=dict(enable_thinking=True, thinking_budget=THINKING),
        response_format={'type':'json_object'}, messages=[dict(role='system',content=prompt),
            dict(role='user',content=json.dumps(payload, ensure_ascii=False, separators=(',',':')))])


def reservation(req):
    return estimate_tokens(req['messages']) + OUTPUT + THINKING + 512


class PilotBudget(Budget):
    def reserve(self, key, stage, messages, output_tokens):
        return super().reserve(key, stage, messages, output_tokens + THINKING)


class InvalidOutput(ValueError):
    def __init__(self, raw):
        self.raw = raw
        super().__init__('incomplete_or_invalid_json')


def generation_payload(view, previous=None, feedback=None):
    if feedback is None:
        return dict(view)
    return dict(view, previous_output=previous, revision_feedback=feedback)


def judge_payload(view, rubric):
    # Deliberate allowlist. Never pass prior messages, scores, reasoning, or feedback.
    return dict(view, candidate_rubric=rubric)


async def process_task(inp, view, pack, call, save):
    """Bounded orchestration, injected I/O enables tests without paid API calls."""
    state = dict(task_id=inp['task_id'], status='not_passed', task_retained=True, rounds=[])
    previous = feedback = None
    for revision in range(MAX_REVISIONS + 1):
        row = dict(revision=revision, status='generating')
        state['rounds'].append(row)
        try:
            selected = await call('generate', revision, generation_payload(view, previous, feedback))
        except InvalidOutput as exc:
            selected = dict(invalid_raw_content=exc.raw)
            errors = ['incomplete_or_invalid_json']
        else:
            errors = validate_selection(selected, inp, pack)
        if errors:
            row.update(status='invalid_generation', errors=errors)
            previous, feedback = selected, dict(kind='structure_errors', errors=errors)
            save(revision, 'validation.json', dict(errors=errors))
            continue
        rubric = compile_rubric(selected, inp, pack)
        save(revision, 'rubric.json', rubric)
        save(revision, 'validation.json', dict(errors=[]))
        try:
            judgment = await call('judge', revision, judge_payload(view, rubric))
        except InvalidOutput:
            row.update(status='invalid_judge', errors=['incomplete_or_invalid_json'])
            state['status'] = 'invalid_judge'
            return state
        verdict = evaluate(judgment, inp['task_id'], {i['id'] for i in rubric['rubric']},
                           {c['candidate_id'] for c in pack['candidates']})
        save(revision, 'evaluation.json', verdict)
        row.update(status=verdict['status'], scores=verdict['scores'], errors=verdict['errors'],
                   zero_dimensions=verdict['zero_dimensions'])
        if verdict['status'] == 'passed':
            state.update(status='passed', accepted_revision=revision)
            return state
        if verdict['status'] == 'invalid_judge':
            state['status'] = 'invalid_judge'
            return state
        previous, feedback = selected, dict(kind='judge_feedback', judgment=judgment)
    state['status'] = 'revision_limit_reached'
    return state


def prepare():
    if (RUN/'manifest.json').exists(): raise SystemExit('Already prepared; frozen artifacts retained')
    api = config(); old = read_json(BASE/'manifest.json'); tasks = []
    for role, path in PROMPTS.items():
        dest = RUN/'prompts'/f'{role}.txt'; dest.parent.mkdir(parents=True,exist_ok=True); dest.write_text(path.read_text())
    for t in old['tasks']:
        assert file_hash(t['input_ref']) == t['input_sha256']
        inp = read_json(t['input_ref']); view, pack = prepare_input(inp)
        d = RUN/'inputs'/t['task_id'].replace(':','__')
        for name, value in [('original',inp),('candidate_input',view),('candidate_audit',pack)]: write_json(d/f'{name}.json',value)
        req = request(view, api, PROMPTS['generate'].read_text())
        assert reservation(req) < 128000
        tasks.append(dict(task_id=t['task_id'], input_ref=str(d/'original.json'), input_sha256=file_hash(d/'original.json'),
            view_ref=str(d/'candidate_input.json'), view_sha256=file_hash(d/'candidate_input.json'),
            audit_ref=str(d/'candidate_audit.json'), audit_sha256=file_hash(d/'candidate_audit.json'),
            previous_input_ref=t['input_ref'], previous_input_sha256=t['input_sha256']))
    before = ledger_snapshot()
    m = dict(version='all-qwen-v13', tasks=tasks, api=api.model_dump(), budget_before=before,
        max_requests=60, max_revisions=MAX_REVISIONS, context_guard=128000, concurrency=2,
        operator_token_cap=CAP, cumulative_cap=before['tokens_accounted_or_reserved']+CAP,
        cap_provenance='Operator safety ceiling, not a user-specified token budget',
        request_prefix='rubric_v13', automatic_network_retries=0, full_run=False,
        prompt_hashes={k:file_hash(RUN/'prompts'/f'{k}.txt') for k in PROMPTS},
        implementation_hashes={str(p):file_hash(p) for p in [Path(__file__), ROOT/'scripts/rubric_candidates.py']},
        review_reference_sha256=file_hash(LEDGER.parent/'review_reference.json'),
        evaluation=dict(dimensions=list(DIMENSIONS), pass_rule='valid_five_scores_and_no_zero',
            invalid_review='stop_not_passed', structure_repairs_consume_revision=True,
            independent_static_review_is_not_a_gate=True, fresh_judge_every_task_and_revision=True),
        pricing=dict(currency='CNY', input_per_million=0.2, output_per_million=2.0,
            source='https://help.aliyun.com/zh/model-studio/qwen3-5-flash', verified_date='2026-10-24',
            accounting='ordinary Beijing <=128k input price; no unverified cache discount'),
        comparison_boundary='Same known ten tasks, no runtime accuracy or unseen-task guarantee')
    write_json(RUN/'manifest.json',m)
    write_json(RUN/'authorization.json',dict(manifest_sha256=file_hash(RUN/'manifest.json'),source='direct_user_instruction',
        scope='Same ten tasks; all Qwen3.5-Flash; fresh context for every review; valid no-zero passes; maximum two revisions; each request below128k; retain original tasks'))
    provenance = read_json(BASE/'public_data_provenance.json')
    provenance.update(destinations={'generate':api.base_url,'judge':api.base_url}, max_requests=60,
        operator_token_cap=CAP, data_unchanged=True, deepseek_used=False)
    write_json(RUN/'public_data_provenance.json',provenance)
    print(json.dumps(dict(prepared=len(tasks), max_requests=60, budget_before=before, operator_cap=CAP),ensure_ascii=False))


async def run():
    m = read_json(RUN/'manifest.json'); api = config()
    assert api.model_dump() == m['api']
    assert read_json(RUN/'authorization.json')['manifest_sha256'] == file_hash(RUN/'manifest.json')
    for path, h in m['implementation_hashes'].items(): assert file_hash(path) == h
    for role, h in m['prompt_hashes'].items(): assert file_hash(RUN/'prompts'/f'{role}.txt') == h
    budget = PilotBudget(LEDGER, m['budget_before']['physical_requests']+m['max_requests'], m['cumulative_cap'])
    sem = asyncio.Semaphore(m['concurrency'])

    async def one(t):
        name = t['task_id'].replace(':','__'); folder = RUN/'tasks'/name
        state = dict(task_id=t['task_id'],status='stopped',task_retained=True)
        try:
            for k,h in [('input_ref','input_sha256'),('view_ref','view_sha256'),('audit_ref','audit_sha256')]: assert file_hash(t[k])==t[h]
            inp,view,pack = read_json(t['input_ref']),read_json(t['view_ref']),read_json(t['audit_ref'])
            def save(revision,filename,data): freeze(folder/f'round_{revision}'/filename,data)
            async def call(role,revision,payload):
                d = folder/f'round_{revision}'/role
                req = request(payload,api,(RUN/'prompts'/f'{role}.txt').read_text())
                upper = reservation(req)
                if upper >= 128000: raise ValueError('context_guard_exceeded')
                freeze(d/'planned_request.json',req)
                sig = identity(req,m['api'],t['input_sha256'])
                key = f"{m['request_prefix']}/{t['task_id']}/{revision}/{role}/{sig}"
                freeze(d/'request_identity.json',dict(signature=sig,request_key=key,
                    request_sha256=file_hash(d/'planned_request.json'),reservation_upper=upper))
                if (d/'response.json').exists(): data = read_json(d/'response.json')
                else:
                    with sqlite3.connect(LEDGER) as db:
                        if db.execute('select 1 from requests where request_key=?',(key,)).fetchone():
                            raise ValueError('unknown_request_no_retry')
                    async with sem:
                        data = await api_request(api,req,budget,key,'v13_'+role,d)
                    print(json.dumps(dict(task_id=t['task_id'],revision=revision,role=role,
                        finish=data['choices'][0]['finish_reason'],usage=data.get('usage')),ensure_ascii=False),flush=True)
                raw = data['choices'][0]['message'].get('content') or ''
                (d/'raw_content.txt').write_text(raw)
                try: out = parse_response(data)
                except (ValueError,KeyError,TypeError) as exc: raise InvalidOutput(raw) from exc
                freeze(d/'output.json',out)
                return out
            state = await process_task(inp,view,pack,call,save)
            if state['status']=='passed':
                revision = state['accepted_revision']
                freeze(RUN/'accepted'/f'{name}.json', read_json(folder/f'round_{revision}'/'rubric.json'))
        except Exception as exc:
            state.update(status='stopped', error_type=type(exc).__name__)
        write_json(folder/'status.json',state)
        print(json.dumps(state,ensure_ascii=False),flush=True)
    await asyncio.gather(*(one(t) for t in m['tasks']))
    print(json.dumps(budget.summary()),flush=True)
    summarize()


def summarize():
    m = read_json(RUN/'manifest.json')
    with sqlite3.connect(f'file:{LEDGER}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        own=[dict(x) for x in db.execute('select * from requests where request_key like ?', (m['request_prefix']+'/%',))]
    requests=[]; states=[]
    for t in m['tasks']:
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert file_hash(t['previous_input_ref']) == t['previous_input_sha256']
        inp=read_json(t['input_ref']);view,pack=prepare_input(inp)
        assert view==read_json(t['view_ref']) and pack==read_json(t['audit_ref'])
        folder=RUN/'tasks'/t['task_id'].replace(':','__')
        state=read_json(folder/'status.json'); states.append(state)
        previous=feedback=None; accepted=[]
        for revision in range(MAX_REVISIONS+1):
            rd=folder/f'round_{revision}'; selected=rubric=None
            for role in ('generate','judge'):
                d=rd/role
                if not (d/'request_identity.json').exists(): continue
                rid=read_json(d/'request_identity.json'); req=read_json(d/'planned_request.json')
                assert file_hash(d/'planned_request.json')==rid['request_sha256']
                assert identity(req,m['api'],t['input_sha256'])==rid['signature']
                assert reservation(req)==rid['reservation_upper']<128000
                assert [x['role'] for x in req['messages']]==['system','user']
                assert req['messages'][0]['content']==(RUN/'prompts'/f'{role}.txt').read_text()
                payload=json.loads(req['messages'][1]['content'])
                if role=='generate': assert payload==generation_payload(view,previous,feedback)
                else: assert payload==judge_payload(view,rubric)
                records=[r for r in own if r['request_key']==rid['request_key']]
                if not records: continue # guard/budget stopped before physical dispatch
                assert len(records)==1
                assert read_json(d/'request.json')==req
                record=records[0]
                entry=dict(task_id=t['task_id'],revision=revision,role=role,reservation_upper=rid['reservation_upper'],
                    ledger_status=record['status'],request_key=rid['request_key'])
                if record['status']!='response':
                    entry.update(unknown_reserved_tokens=record['reserved'],reserved_cost_cny=
                        ((record['reserved']-OUTPUT-THINKING)*m['pricing']['input_per_million']+
                        (OUTPUT+THINKING)*m['pricing']['output_per_million'])/1e6)
                    requests.append(entry);continue
                response=read_json(d/'response.json');usage=response['usage']
                assert json.loads(record['usage'])==usage
                assert usage['total_tokens']==usage['prompt_tokens']+usage['completion_tokens']==record['reserved']
                assert usage['total_tokens']<=rid['reservation_upper']
                entry.update({k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')})
                entry.update(finish=response['choices'][0]['finish_reason'],
                    estimated_cny=(usage['prompt_tokens']*m['pricing']['input_per_million']+usage['completion_tokens']*m['pricing']['output_per_million'])/1e6)
                requests.append(entry)
                if not (d/'output.json').exists():
                    if role=='generate':
                        previous=dict(invalid_raw_content=response['choices'][0]['message'].get('content') or '')
                        feedback=dict(kind='structure_errors',errors=['incomplete_or_invalid_json'])
                    continue
                output=parse_response(response);assert output==read_json(d/'output.json')
                if role=='generate':
                    selected=output;errors=validate_selection(output,inp,pack)
                    assert read_json(rd/'validation.json')['errors']==errors
                    if errors:
                        previous,feedback=output,dict(kind='structure_errors',errors=errors)
                    else:
                        rubric=compile_rubric(output,inp,pack);assert rubric==read_json(rd/'rubric.json')
                else:
                    result=evaluate(output,t['task_id'],{i['id'] for i in rubric['rubric']},{c['candidate_id'] for c in pack['candidates']})
                    assert result==read_json(rd/'evaluation.json')
                    if result['status']=='passed':accepted.append(revision)
                    previous,feedback=selected,dict(kind='judge_feedback',judgment=output)
            if accepted:
                assert not (folder/f'round_{revision+1}').exists()
        assert len(accepted)<=1
        if accepted:
            assert state['status']=='passed' and state['accepted_revision']==accepted[0]
            assert read_json(RUN/'accepted'/f"{t['task_id'].replace(':','__')}.json")==read_json(folder/f'round_{accepted[0]}'/'rubric.json')
        else: assert state['status']!='passed'
    assert len(requests)==len(own)
    known=[r for r in requests if r['ledger_status']=='response']
    total=sum(r['estimated_cny'] for r in known); unknown=sum(r.get('unknown_reserved_tokens',0) for r in requests)
    passed=sum(s['status']=='passed' for s in states)
    first=sum(s['status']=='passed' and s['accepted_revision']==0 for s in states)
    metrics=dict(tasks=10,first_pass=first,first_pass_rate=first/10,final_pass=passed,final_pass_rate=passed/10,
        physical_requests=len(own),confirmed_tokens=sum(r['total_tokens'] for r in known),unknown_reserved_tokens=unknown,
        known_estimated_cny=total,unknown_reserved_cny=sum(r.get('reserved_cost_cny',0) for r in requests),
        cost_per_passed_rubric_cny=total/passed if passed else None,
        cost_per_pass_basis='all generation/review/repair attempts divided by final accepted count; confirmed usage only',
        max_request_guard=max(r['reservation_upper'] for r in requests),
        max_actual_input=max((r['prompt_tokens'] for r in known),default=0),
        original_tasks_retained=10,static_review_is_not_a_gate=True,runtime_accuracy_measured=False)
    assert metrics['confirmed_tokens']+unknown<=CAP
    by_stage={f'{kind}_{role}':dict(calls=sum(r['role']==role and (r['revision']==0)==(kind=='initial') for r in requests),
        known_estimated_cny=sum(r['estimated_cny'] for r in known if r['role']==role and (r['revision']==0)==(kind=='initial')))
        for kind in ('initial','revision') for role in ('generate','judge')}
    write_json(RUN/'cost_report.json',dict(metrics=metrics,stages=by_stage,tasks=states,requests=requests,
        actual_bill_verified=False,codex_development_usage_included=False,pricing=m['pricing']))
    write_json(RUN/'retention_manifest.json',dict(original_tasks_retained=True,tasks=[dict(task_id=s['task_id'],
        status=s['status'],accepted_revision=s.get('accepted_revision'),original_ref=t['input_ref']) for s,t in zip(states,m['tasks'])]))
    print(json.dumps(metrics,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run','summarize']);args=p.parse_args()
    if args.action=='prepare':prepare()
    elif args.action=='run':asyncio.run(run())
    else:summarize()
