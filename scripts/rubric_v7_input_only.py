"""Authorized matched v7 replay; input-only reduction, shared ledger, no retries."""
from __future__ import annotations
import argparse, asyncio, copy, hashlib, json, os, sqlite3
from pathlib import Path
import rubric_pilot as pilot
from rubric_pilot import ROOT,read_json,write_json,file_hash,unique_pairs,validate_output,load_config,validate_config,APIConfig,Budget,api_request
from estimate_rubric_v7_input_only import prune_examples
BASE=ROOT/'runs/rubric_pilot10_20261024'
PLAN=ROOT/'runs/rubric_v7_input_only_plan_20261024'
RUN=ROOT/'runs/rubric_v7_input_only_20261024'
CAP=954558
def signature(request,source_sha):
    return hashlib.sha256((json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(',',':'))+source_sha).encode()).hexdigest()

def prepare():
    if (RUN/'manifest.json').exists():raise SystemExit('Already prepared; use frozen manifest')
    before=Budget(BASE/'budget.sqlite',110,CAP).summary()
    assert before['tokens_accounted_or_reserved']==794558 and before['physical_requests']==98
    old=read_json(BASE/'manifest.json');rows=[]
    for t in old['tasks']:
        name=t['task_id'].replace(':','__');candidate=read_json(PLAN/'inputs'/name/'candidate_request.json')
        original=read_json(BASE/'v7'/name/'request.json')
        assert candidate['messages'][0]==original['messages'][0]
        assert {k:v for k,v in candidate.items() if k!='messages'}=={k:v for k,v in original.items() if k!='messages'}
        a=json.loads(original['messages'][1]['content']);b=json.loads(candidate['messages'][1]['content'])
        expected=copy.deepcopy(a)
        for source in expected['sources']:
            if source['kind']=='public_contract':source['text']=json.dumps(prune_examples(json.loads(source['text'])),ensure_ascii=False)
        assert expected['task']==next(s['text'] for s in expected['sources'] if s['kind']=='task')
        del expected['task'];assert expected==b
        inp=BASE/t['input_ref'];assert file_hash(inp)==t['input_sha256']
        dest=RUN/'initial'/name;write_json(dest/'planned_request.json',candidate)
        rows.append(dict(task_id=t['task_id'],input_ref=str(inp),input_sha256=t['input_sha256'],
            request_sha256=file_hash(dest/'planned_request.json'),cache_signature=signature(candidate,t['input_sha256']),
            reservation_upper=len(json.dumps(candidate['messages'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())+128+512+candidate['max_tokens']))
    manifest=dict(tasks=rows,model=old['model'],base_url=old['base_url'],budget_before=before,
       budget={'max_total_tokens':CAP,'new_experiment_cap':160000,'max_physical_requests':110,'automatic_retries':0},
       authorization={'source':'direct_user_message','text':'批准这次16万tokens的预算，按这个方案继续用DeepSeek试跑同样的10道题，保留v7的约束，重点看能不能保持8/10的质量，同时统计实际用量，先不要全量跑。'},
       plan=str(ROOT/'docs/rubric_v7_input_only_plan_zh.md'),full_run=False,
       baseline_review_sha256=file_hash(BASE/'v7/source_review.json'),
       review_reference_sha256=file_hash(BASE/'review_reference.json'),
       gate='initial >=8/10 AND no regression among baseline accepted 8; otherwise stop paid repairs',
       max_repairs=2,concurrency=1)
    write_json(RUN/'manifest.json',manifest)
    (RUN/'initial/prompt.txt').write_text(original['messages'][0]['content'])
    write_json(RUN/'authorization.json',manifest['authorization'])
    print(json.dumps({'prepared':10,'before':before,'cap':CAP,'remote_calls':0},ensure_ascii=False))

def validation(stage):
    m=read_json(RUN/'manifest.json');rows=[]
    tasks=m['tasks'] if stage=='initial' else [t for t in m['tasks'] if t['task_id'] in read_json(RUN/'repair_plan.json')['task_ids']]
    for t in tasks:
        d=RUN/stage/t['task_id'].replace(':','__')
        try:
            data=read_json(d/'response.json');c=data['choices'][0]
            (d/'raw_content.txt').write_text(c['message']['content'] or '')
            if c['finish_reason']!='stop':raise ValueError('finish_reason='+str(c['finish_reason']))
            out=json.loads(c['message']['content'],object_pairs_hook=unique_pairs)
            errors=validate_output(out,read_json(Path(t['input_ref'])))
            write_json(d/'rubric.json',out)
            rows.append(dict(task_id=t['task_id'],items=len(out.get('rubric',[])),errors=errors,usage=data.get('usage')))
        except Exception as e:rows.append(dict(task_id=t['task_id'],items=0,errors=[type(e).__name__+':'+str(e)]))
    pilot.dump_rows(RUN/stage/'validation.jsonl',rows)
    summary={'tasks':len(rows),'structure_pass':sum(not r['errors'] for r in rows),'items':sum(r['items'] for r in rows)}
    write_json(RUN/stage/'validation_summary.json',summary);print(json.dumps(summary))

async def run(stage):
    m=read_json(RUN/'manifest.json');cfg=load_config('/home/bjx/app-agent/.env');validate_config(cfg)
    assert (cfg.model,cfg.base_url)==(m['model'],m['base_url'])
    tasks=m['tasks']
    if stage=='repair':
        review=read_json(RUN/'initial/source_review.json')
        accepted={r['task_id'] for r in review['tasks'] if r['status']=='draft_review_pass_with_notes'}
        baseline={r['task_id'] for r in read_json(BASE/'v7/source_review.json')['tasks'] if r['status']=='draft_review_pass_with_notes'}
        assert len(accepted)>=8 and baseline<=accepted,'Quality gate forbids further paid calls'
        plan=read_json(RUN/'repair_plan.json');assert len(plan['task_ids'])<=2 and len(set(plan['task_ids']))==len(plan['task_ids'])
        assert not accepted.intersection(plan['task_ids'])
        tasks=[t for t in tasks if t['task_id'] in plan['task_ids']]
    os.environ['RUBRIC_V7_INPUT_ONLY_KEY']=cfg.api_key
    api=APIConfig(base_url=cfg.base_url,model=cfg.model,api_key_env='RUBRIC_V7_INPUT_ONLY_KEY',timeout=240,temperature=0)
    b=Budget(BASE/'budget.sqlite',m['budget']['max_physical_requests'],m['budget']['max_total_tokens'])
    for t in sorted(tasks,key=lambda t:t['reservation_upper'],reverse=True):
        d=RUN/stage/t['task_id'].replace(':','__');req=read_json(d/'planned_request.json')
        assert file_hash(Path(t['input_ref']))==t['input_sha256']
        if stage=='initial':assert file_hash(d/'planned_request.json')==t['request_sha256']
        else:assert req['messages'][0]['content']==(RUN/'initial/prompt.txt').read_text()
        sig=signature(req,t['input_sha256']);key='v7_input_only/'+stage+'/'+t['task_id']+'/'+sig
        if (d/'response.json').exists():
            assert read_json(d/'request_identity.json')['signature']==sig
            continue
        with sqlite3.connect(BASE/'budget.sqlite') as db:
            if db.execute('select 1 from requests where request_key=?',(key,)).fetchone():raise RuntimeError('previous_reservation_no_retry')
        write_json(d/'request_identity.json',{'signature':sig,'request_key':key,'source_sha256':t['input_sha256']})
        data=await api_request(api,req,b,key,'rubric_v7_input_only_'+stage,d)
        print(json.dumps({'task_id':t['task_id'],'stage':stage,'finish':data['choices'][0]['finish_reason'],'usage':data.get('usage')},ensure_ascii=False),flush=True)
    validation(stage)
    print(json.dumps(b.summary()))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run','validate','render']);p.add_argument('--stage',choices=['initial','repair'],default='initial');a=p.parse_args()
    if a.action=='prepare':prepare()
    elif a.action=='run':asyncio.run(run(a.stage))
    elif a.action=='validate':validation(a.stage)
    else:
        pilot.RUN=RUN
        pilot.render(a.stage)
