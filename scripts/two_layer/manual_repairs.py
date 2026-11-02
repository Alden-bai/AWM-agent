"""Evidence-led fixes of shared field-update/create templates; preserves all prior versions."""
import asyncio, copy, json, sys
from pathlib import Path
from run import *


def q(v):return "'"+str(v).replace("'","''")+"'"


def config_for(task):
    if task['scenario']=='e_commerce_10':
        text='Great taste, mixes easily, and no stomach issues even with my lactose sensitivity'
        fields={'sku_code':'WHEY-ISOLATE-CHOC-2LB','rating':5,'review_text':text,'status':'pending'}
        calls=[{'tool':'search_skus','arguments':{}},{'tool':'create_review','arguments':fields}]
        anchor="r.id=json_extract((SELECT value FROM evidence.tool_results WHERE step=1),'$.review_id') AND r.id NOT IN (SELECT id FROM initial.reviews) AND r.user_id=1"
        req=[]
        for key,value,quote in [('sku_code',fields['sku_code'],"Submit a new product review for SKU 'WHEY-ISOLATE-CHOC-2LB'"),('rating',5,'5-star rating'),('review_text',text,"the text '"+text+"'"),('status','pending',"set the review moderation status to 'pending'")]:
            col='s.sku_code' if key=='sku_code' else 'r.'+key
            req.append({'id':key,'source_quote':quote,'description':'Requested '+key+' on the same actual newly created current-user review','sql':f'SELECT EXISTS(SELECT 1 FROM reviews r JOIN sku s ON r.sku_id=s.id WHERE {anchor} AND {col}={q(value)})'})
        negatives=[{'id':'wrong_object','target':'sku_code','kind':'mutation','step':1,'path':['sku_code'],'value':{'$ref':0,'path':['skus',0,'sku_code']},'reason':'Use an existing different SKU returned by the public catalog. Independently verify selected SKU differs.'},
                   {'id':'wrong_rating','target':'rating','kind':'mutation','step':1,'path':['rating'],'value':4},
                   {'id':'wrong_text','target':'review_text','kind':'mutation','step':1,'path':['review_text'],'value':'Different review text'},
                   {'id':'wrong_status','target':'status','kind':'mutation','step':1,'path':['status'],'value':'approved'},
                   {'id':'omitted_create','target':'sku_code','kind':'omission','step':1}]
        checks=[{'step':0,'description':'Public SKU list complete within default 50, IDs and codes exactly match initial DB; unfiltered read is unchanged by later review insertion.','sql':"SELECT (SELECT count(*) FROM sku)<=50 AND (SELECT json_extract(value,'$.total') FROM evidence.tool_results WHERE step=0)=(SELECT count(*) FROM sku) AND (SELECT count(*) FROM json_each((SELECT value FROM evidence.tool_results WHERE step=0),'$.skus'))=(SELECT count(*) FROM sku) AND NOT EXISTS(SELECT id,sku_code FROM sku EXCEPT SELECT json_extract(value,'$.id'),json_extract(value,'$.sku_code') FROM json_each((SELECT value FROM evidence.tool_results WHERE step=0),'$.skus'))"},
                {'step':1,'description':'Created response identity and every requested field match the committed new row, owner and SKU.','sql':f"SELECT EXISTS(SELECT 1 FROM reviews r JOIN sku s ON s.id=r.sku_id JOIN evidence.tool_results t ON t.step=1 WHERE {anchor} AND r.sku_id=json_extract(t.value,'$.sku_id') AND r.user_id=json_extract(t.value,'$.user_id') AND r.rating=json_extract(t.value,'$.rating') AND r.review_text=json_extract(t.value,'$.review_text') AND r.status=json_extract(t.value,'$.status'))"}]
        boundaries=[{'id':'wrong_object','description':'Wrong existing SKU','status':'covered','case_ids':['wrong_object'],'evidence':'Actual unfiltered search_skus response; selected first SKU is independently checked different from requested code.'},{'id':'fixed_owner','description':'Owner is fixed by create_review public API context','status':'not_applicable_with_evidence','case_ids':[],'evidence':'No user_id parameter in create_review. Implementation unconditionally sets user_id=1; omission checks existence/owner together.'},{'id':'time_output','description':'No explicit time/filter/set/final-format requirement','status':'not_applicable_with_evidence','case_ids':[],'evidence':'Complete task specifies only new review SKU, rating, text and moderation status.'}]
    else:
        fields={'competency_id':340,'level':'intermediate','category':'pipefitting','short_description':'Install and test residential copper and PEX water lines'}
        calls=[{'tool':'list_competencies','arguments':{}},{'tool':'update_competency','arguments':fields}]
        quotes={'level':"update its level from 'basic' to 'intermediate'",'category':"set category to 'pipefitting'",'short_description':"revise the short description to 'Install and test residential copper and PEX water lines'"}
        req=[{'id':k,'source_quote':quotes[k],'description':'Requested '+k+' of exact target competency 340','sql':f'SELECT EXISTS(SELECT 1 FROM competencies WHERE id=340 AND {k}={q(fields[k])})'} for k in quotes]
        negatives=[{'id':'wrong_level','target':'level','kind':'mutation','step':1,'path':['level'],'value':'advanced'},
                   {'id':'wrong_category','target':'category','kind':'mutation','step':1,'path':['category'],'value':'plumbing'},
                   {'id':'wrong_description','target':'short_description','kind':'mutation','step':1,'path':['short_description'],'value':'Different description'},
                   {'id':'wrong_object','target':'level','kind':'mutation','step':1,'path':['competency_id'],'value':{'$ref':0,'path':['competencies',0,'id']},'reason':'Existing different competency from public list; actual returned ID independently differs from 340.'},
                   {'id':'omitted_update','target':'level','kind':'omission','step':1}]
        checks=[{'step':0,'description':'Unfiltered competency list exactly matches initial records for IDs/names/levels/categories/descriptions.','sql':"SELECT (SELECT count(*) FROM json_each((SELECT value FROM evidence.tool_results WHERE step=0),'$.competencies'))=(SELECT count(*) FROM initial.competencies) AND NOT EXISTS(SELECT id,name,level,category,short_description FROM initial.competencies EXCEPT SELECT json_extract(value,'$.id'),json_extract(value,'$.name'),json_extract(value,'$.level'),json_extract(value,'$.category'),json_extract(value,'$.short_description') FROM json_each((SELECT value FROM evidence.tool_results WHERE step=0),'$.competencies'))"},
                {'step':1,'description':'Update response identity and changed fields exactly match final target 340.','sql':"SELECT EXISTS(SELECT 1 FROM competencies c JOIN evidence.tool_results t ON t.step=1 WHERE c.id=340 AND c.id=json_extract(t.value,'$.id') AND c.level=json_extract(t.value,'$.level') AND c.category=json_extract(t.value,'$.category') AND c.short_description=json_extract(t.value,'$.short_description'))"}]
        boundaries=[{'id':'wrong_object','description':'Wrong existing competency from public lookup','status':'covered','case_ids':['wrong_object'],'evidence':'list_competencies actual response, public result ID selected and verified different from 340.'},{'id':'other','description':'No time, batch or explicit output-format condition','status':'not_applicable_with_evidence','case_ids':[],'evidence':'Full task only updates three fields of competency ID 340. Other-row invariance is not silently added as a task condition.'}]
    sources=[{'step':1,'path':[k],'kind':'task_literal','quote':next((r['source_quote'] for r in req if r['id']==k),'For the plumbing competency with ID 340'),'reason':'Explicit task value, numeric 5-star maps to 5.'} for k in fields]
    return {'requirements':req,'clauses':[{'quote':r['source_quote'],'requirements':[r['id']],'interpretation':'requirement'} for r in req],'ambiguities':[],'positive':{'calls':calls,'answer':'Done.'},'negatives':negatives,'equivalent_positive':None,'boundaries':boundaries,'tool_checks':checks,'argument_sources':sources,'notes':['Single-target field template; no constraints on unspecified unrelated global row counts. Cases contain at most one write, so all field checks reference the same independently identified object. Prior model-made wrong IDs and extraneous conditions are retired, not relabeled to match a Judge.']}


async def repair(run,ledger):
    run=Path(run);m=read_json(run/'manifest.json');task=next(t for t in m['tasks'] if t['ledger_id']==ledger)
    assets=configure_runtime(run);root=run/'tasks'/ledger;folder=root/'manual_v2';folder.mkdir(exist_ok=True)
    prior=read_json(root/'result.json');save_json(folder/'prior_result.json',prior)
    spec=config_for(task);save_json(folder/'config.json',spec)
    # Independently re-read the original task and constrain only its explicit outcomes.
    independent=copy.deepcopy(spec);independent['requirements']=[{**r,'id':'independent_'+r['id']} for r in spec['requirements']]
    independent['review_origin']='Root source-and-database review of every task clause; SQL shares template so this is not a claim of statistically independent model truth.'
    save_json(folder/'independent.json',independent)
    cases,replay=await execute_cases(run,task,spec,independent,folder/'execution')
    result={**prior,'evidence_dir':str(folder),'state':'pending_build','layer1':'pending','layer2':'pending','reasons':[],'retired_judged_cases':prior.get('judged_cases',[]),'judged_cases':[],'requirements_count':len(spec['requirements']),'independent_requirements_count':len(independent['requirements']),'case_count':len(cases)}
    matrix=coverage(spec,cases);result['coverage']=matrix
    if not replay['passed'] or not all(c['label_valid'] for c in cases) or not all(x['status']=='covered' for x in matrix):
        result['reasons']=['manual_template_execution_or_coverage_gap'];save_json(folder/'result.json',result);print(ledger,result['reasons'],flush=True);return
    # Validate wrong-object IDs from real returned catalog/list, not from the audit DB.
    wrong=next(c for c in cases if c['name']=='wrong_object')
    actual=[tc['arguments'] for row in wrong['trajectory'] for tc in row.get('tool_calls',[]) if tc['name']=='call_tool'][-1]
    actual_args=json.loads(actual['arguments']);target='sku_code' if task['scenario']=='e_commerce_10' else 'competency_id'
    assert actual_args[target]!=spec['positive']['calls'][1]['arguments'][target]
    # Read-only lookup plus per-write checks and fresh replay establish dependency scope.
    assert all(all(x['passed'] for x in c['tool_checks']) for c in cases if c['expected'])
    model=Model(run);contract=compact_contract(next(r['api_spec'] for r in assets['gen_spec.jsonl'] if r['scenario']==task['scenario']))
    review=await model.json(folder/'review','review',prompts.REVIEW+' The initial independent model extraction remains in ../independent_0; this revision was repaired by the root auditor from the original task and actual evidence. Check that the revised conditions remove only unsupported additions, never required clauses.','' if False else {'task':task['task'],'public_contract':contract,'schema':next(r['db_schema'] for r in assets['gen_db.jsonl'] if r['scenario']==task['scenario']),'initial_state':relevant_state(run/'environments'/task['scenario']/'initial.db',spec,independent,cases),'first_extraction':spec,'independent_extraction':independent,'cases':[review_case(c) for c in cases],'replay':review_case(replay),'coverage':matrix},max_tokens=6000)
    result['review']=review
    if not review.get('approved'):
        result['reasons']=['manual_template_semantic_review_not_approved'];save_json(folder/'result.json',result);print(ledger,review,flush=True);return
    result['layer1']='passed_task_dependencies'
    freeze={'spec_sha256':digest(spec),'independent_sha256':digest(independent),'review_sha256':digest(review),'cases':[{'name':c['name'],'expected':c['expected'],'path':c['path'],'label_sha256':digest(c)} for c in cases],'all_keep_gates_before_judge':True,'frozen_at':now(),'revision_reason':'Public wrong-object lookup and removal of unsupported task conditions; all cases freshly executed and all Judge slots new.'}
    save_json(folder/'frozen_formal_cases.json',freeze);result['formal_cases']=len(cases)
    entry=next(e for e in assets['gen_verifier.jsonl'] if e['scenario']==task['scenario'] and e['task_idx']==task['task_idx'])
    no_error=True
    for c in cases:
        ext=extract_official(entry,c['path']);cr={'name':c['name'],'expected':c['expected'],'slots':[],'path':c['path']};result['judged_cases'].append(cr)
        if not ext['valid']:result['reasons'].append('sql_error');no_error=False;continue
        for slot in range(5):
            f=Path(c['path'])/'judge'/str(slot);v=await model.judge(f,task['task'],ext['details'],c['trajectory']);cr['slots'].append({'slot':slot,**v})
            if not v['valid']:no_error=False;result['reasons'].append('invalid_judge_output')
            elif v['accepted']!=c['expected']:
                no_error=False;result['state']='review';result['reasons'].append('manual_template_judge_disagreement');break
    if no_error:result.update(state='keep',layer2='passed_finite_suite',reasons=[])
    elif result['state']!='review':result.update(state='blocked_run',layer2='invalid_or_incomplete_judge')
    save_json(folder/'result.json',result);save_json(root/'previous_result_before_manual_v2.json',prior);save_json(root/'result.json',result)
    print(ledger,result['state'],result['reasons'],flush=True)

async def main():
    run=ROOT/'cleaning_runs/two_layer_pilot100_v1'
    # Separate subprocesses prevent official transport wrapper overlap.
    await repair(run,sys.argv[1])
if __name__=='__main__':asyncio.run(main())
