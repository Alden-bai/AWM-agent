"""Fresh reproduction of an old explicit-answer disagreement, independent DB labels."""
import asyncio,copy,json,sqlite3
from pathlib import Path
from run import *


def independent_answer(dbpath):
 queries={
  'scope':"SELECT a.id,a.applicant_id,a.program_cycle_id FROM applications a JOIN program_cycles pc ON pc.id=a.program_cycle_id JOIN programs p ON p.id=pc.program_id JOIN institutions i ON i.id=p.institution_id JOIN admission_cycles c ON c.id=pc.cycle_id WHERE p.name='MBA Online' AND i.name='Metro Business School' AND c.name='Spring 2025' ORDER BY a.id",
  'missing':"SELECT d.id AS checklist_definition_id,d.name FROM checklist_item_definitions d WHERE d.program_cycle_id=? AND d.is_required=1 AND NOT EXISTS(SELECT 1 FROM application_checklist_items x WHERE x.application_id=? AND x.checklist_definition_id=d.id AND x.is_completed=1) ORDER BY d.id",
  'rejected':"SELECT d.id AS document_id,t.name AS document_type_name,d.verification_comments FROM documents d JOIN document_types t ON t.id=d.document_type_id WHERE d.application_id=? AND d.verification_status='rejected' ORDER BY d.id"}
 with sqlite3.connect(f'file:{dbpath}?mode=ro',uri=True) as db:
  db.row_factory=sqlite3.Row;scope=[dict(r) for r in db.execute(queries['scope'])];rows=[];evidence=[]
  for a in scope:
   missing=[dict(r) for r in db.execute(queries['missing'],(a['program_cycle_id'],a['id']))];rejected=[dict(r) for r in db.execute(queries['rejected'],(a['id'],))]
   evidence.append({'scope_row':a,'missing':missing,'rejected':rejected})
   if missing or rejected:rows.append({'application_id':a['id'],'applicant_id':a['applicant_id'],'missing_required_checklist_items':missing,'rejected_documents':rejected})
 return {'applications':rows},{'queries':queries,'scope_rows':scope,'per_application':evidence}

async def main():
 run=ROOT/'cleaning_runs/two_layer_pilot100_v1';m=read_json(run/'manifest.json');task=next(t for t in m['tasks'] if t['ledger_id']=='row154:task9');assets=configure_runtime(run)
 root=run/'tasks'/task['ledger_id'];folder=root/'explicit_output_v2';folder.mkdir(exist_ok=True)
 spec=read_json(root/'round_0/requirements.json');recipe=copy.deepcopy(spec['positive']);recipe.update(name='positive',expected=True,answer={'$ref':3})
 initial=run/'environments'/task['scenario']/'initial.db';expected,evidence=independent_answer(initial);assert expected['applications']
 save_json(folder/'reference.json',{'task':task['task'],'expected':expected,'evidence':evidence,'basis':'Task literal institution/program/cycle; complete per-application missing-required or rejected-document union. No official verifier read during this independent calculation.'})
 traces=[]
 for name in ['positive','positive_replay']:
  f=folder/name
  trace=read_json(f/'trajectory.json') if (f/'trajectory.json').exists() else await runtime.execute_case(run,task,{**recipe,'name':name},initial,f)
  assert not trace['failure'];actual=json.loads(trace['final_answer']);assert actual==expected,(actual,expected)
  assert full_state(f/'initial.db')==full_state(f/'final.db')
  traces.append(trace)
 base=traces[0];cases=[]
 for name,answer,label in [('positive',base['final_answer'],True),('empty_answer','{}',False),('wrong_applicant',None,False)]:
  f=folder/name
  if name!='positive':
   f.mkdir(exist_ok=True)
   for n in ['initial','final']:backup(folder/'positive'/(n+'.db'),f/(n+'.db'))
   t=copy.deepcopy(base)
   if name=='wrong_applicant':
    variant=copy.deepcopy(expected);variant['applications'][0]['applicant_id']=-1;answer=json.dumps(variant,ensure_ascii=False)
   t['final_answer']=answer;t['messages']=[{'role':'assistant','content':answer}];t['trajectory'][-1]['content']=answer;t['case']=name;t['variant_origin']='Only final answer varied over the same frozen real read-only evidence, per protocol.'
   save_json(f/'trajectory.json',t)
  else:t=base
  actual=json.loads(answer);correct=actual==expected;assert correct==label
  c={'name':name,'expected':label,'path':str(f),'actual_answer':actual,'independent_reference':expected,'label_valid':True,'trajectory':t['trajectory'],'violated':['return applicant IDs and missing or invalid checklist items'] if not label else []}
  save_json(f/'label.json',c);cases.append(c)
 freeze={'frozen_at':now(),'cases':[{'name':c['name'],'expected':c['expected'],'path':c['path'],'label_sha256':digest(c)} for c in cases],'all_keep_gates_before_judge':False,'scope':'Targeted recheck of known explicit-output disagreement; insufficient boundary coverage to certify keep by itself.'};save_json(folder/'frozen_formal_cases.json',freeze)
 entry=next(e for e in assets['gen_verifier.jsonl'] if e['scenario']==task['scenario'] and e['task_idx']==task['task_idx'])
 model=Model(run);prior=read_json(root/'result.json');result={**prior,'evidence_dir':str(folder),'formal_cases':3,'judged_cases':[],'targeted_output_reference':str(folder/'reference.json'),'targeted_output_input_scope':'Fresh read-only tool execution, two positive replays; final-answer variants share the same real evidence.'}
 for c in cases:
  ext=extract_official(entry,c['path']);assert ext['valid'];cr={'name':c['name'],'expected':c['expected'],'path':c['path'],'slots':[]};result['judged_cases'].append(cr)
  for slot in range(5):
   jf=Path(c['path'])/'judge'/str(slot);v=await model.judge(jf,task['task'],ext['details'],c['trajectory']);cr['slots'].append({'slot':slot,**v})
   if v['valid'] and v['accepted']!=c['expected']:
    audit=await model.json(Path(c['path'])/'dispute'/str(slot),'dispute',prompts.DISPUTE,{'task':task['task'],'reference':read_json(folder/'reference.json'),'case':c,'actual_official_request':read_json(jf/'official_request.json'),'judge':v,'specific_requirement':'The user explicitly requires returned applicant IDs and missing/invalid checklist items. Empty/wrong IDs in the final answer do not deliver those results. Task does not merely ask to run a tool.'},max_tokens=5000)
    cr['dispute']=audit
    if audit.get('confirmed_misclassification') and audit.get('label_correct') and audit.get('adapter_correct') and not audit.get('ambiguities') and not audit.get('label_errors'):
     result.update(state='reject',layer2='judge_defect',reasons=['confirmed_explicit_answer_false_accept' if not c['expected'] else 'confirmed_explicit_answer_false_reject'],early_stop='not_run_due_to_confirmed_defect')
    else:result.update(state='review',reasons=['explicit_output_dispute_not_resolved'])
    save_json(folder/'result.json',result);save_json(root/'before_explicit_output_v2.json',prior);save_json(root/'result.json',result);print(result['state'],result['reasons'],flush=True);return
 if all(s['valid'] and s['accepted']==c['expected'] for c in result['judged_cases'] for s in c['slots']):result.update(state='pending_build',reasons=['targeted_output_recheck_passed_but_full_boundary_suite_not_complete'])
 else:result.update(state='blocked_run',reasons=['targeted_output_recheck_has_invalid_judge_slots'])
 save_json(folder/'result.json',result);save_json(root/'before_explicit_output_v2.json',prior);save_json(root/'result.json',result);print(result['state'],result['reasons'],flush=True)

if __name__=='__main__':asyncio.run(main())
