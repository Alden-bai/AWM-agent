"""Free local reproductions and deterministic input audit; no model calls."""
import ast
from run import *


async def main():
 run=ROOT/'cleaning_runs/two_layer_pilot100_v1';m=read_json(run/'manifest.json');assets=configure_runtime(run)
 scenario='education_training_management_1';initial=run/'environments'/scenario/'initial.db'
 code=next(x['full_code'] for x in assets['gen_envs.jsonl'] if x['scenario']==scenario)
 operations={n.name:ast.get_source_segment(code,n) for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
 queries={
 'owner':'SELECT * FROM apprentices WHERE id=552',
 'hours':"SELECT * FROM work_log_entries WHERE apprentice_id=552 AND work_date BETWEEN '2025-01-01' AND '2025-03-31'",
 'competencies':"SELECT * FROM competency_records WHERE apprentice_id=552 AND record_date BETWEEN '2025-01-01' AND '2025-03-31' AND rating>=3",
 'evaluations':"SELECT * FROM class_evaluations WHERE apprentice_id=552 AND evaluation_date BETWEEN '2025-01-01' AND '2025-03-31'",
 'all_hours':'SELECT * FROM work_log_entries WHERE apprentice_id=552',
 'program':'SELECT * FROM programs WHERE id=32',
 'required_competencies':'SELECT * FROM program_required_competencies WHERE program_id=32',
 'required_classes':'SELECT * FROM program_required_classes WHERE program_id=32',
 'all_competencies':'SELECT * FROM competency_records WHERE apprentice_id=552',
 'all_evaluations':'SELECT * FROM class_evaluations WHERE apprentice_id=552',
 'missing_competencies':'SELECT * FROM program_required_competencies r WHERE program_id=32 AND is_mandatory=1 AND NOT EXISTS(SELECT 1 FROM competency_records c WHERE c.apprentice_id=552 AND c.competency_id=r.competency_id AND c.rating>=3)',
 'missing_classes':"SELECT * FROM program_required_classes r WHERE program_id=32 AND is_mandatory=1 AND NOT EXISTS(SELECT 1 FROM class_evaluations e WHERE e.apprentice_id=552 AND e.class_id=r.class_id AND lower(e.pass_fail)='pass')"}
 with sqlite3.connect(initial) as db:
  db.row_factory=sqlite3.Row;facts={k:[dict(r) for r in db.execute(q)] for k,q in queries.items()}
 expected={'total_work_hours':sum(r['hours_worked'] for r in facts['hours']),'completed_competencies_count':len({r['competency_id'] for r in facts['competencies']}),'average_evaluation_score':sum(r['score'] for r in facts['evaluations'])/len(facts['evaluations'])}
 assert facts['owner'][0]['user_id']==2 and expected['total_work_hours']>0
 assert sum(r['hours_worked'] for r in facts['all_hours'])>=facts['program'][0]['required_hours_on_job'] and not facts['missing_competencies'] and not facts['missing_classes']
 findings=clean_data.rows(run/'confirmed_defects.jsonl')
 for idx in (7,9):
  task=next(t for t in m['tasks'] if t['scenario']==scenario and t['task_idx']==idx);folder=run/'confirmed_environment_defects'/task['ledger_id'];folder.mkdir(exist_ok=True)
  maincall={'tool':'get_apprentice_dashboard_summary','arguments':{'apprentice_id':552,'start_date':'2025-01-01','end_date':'2025-03-31','completed_rating_threshold':3}} if idx==7 else {'tool':'get_program_completion_status','arguments':{'program_id':32,'apprentice_id':552}}
  calls=[maincall,{'tool':'list_work_log_entries','arguments':{'apprentice_id':552}},{'tool':'list_competency_records','arguments':{'apprentice_id':552}},{'tool':'list_class_evaluations_for_apprentice','arguments':{'apprentice_id':552}}]
  for repeat in range(2):
   f=folder/str(repeat);trace=read_json(f/'trajectory.json') if (f/'trajectory.json').exists() else await runtime.execute_case(run,task,{'name':'dependency_read_probe','calls':calls,'answer':{'$ref':0},'expected':True},initial,f)
   assert not trace['failure'],trace['failure']
   actual=json.loads(trace['final_answer'])
   for row in trace['trajectory'][2:5]:
    assert not row.get('is_error')
    assert all(v==[] for v in json.loads(row['tool_response']['content']).values())
   if idx==7:assert actual['total_work_hours']==0 and actual['completed_competencies_count']==0 and actual['average_evaluation_score'] is None
   else:assert actual['total_hours_on_job']==0 and actual['is_program_completed'] is False
   assert full_state(f/'initial.db')==full_state(f/'final.db')
   save_json(f/'independent_check.json',{'actual':actual,'expected_dashboard':expected,'all_hours':sum(r['hours_worked'] for r in facts['all_hours']),'all_program_prerequisites_met':True,'queries':queries,'rows':facts,'scope':'Dependency diagnostic, not a successful complete task trajectory. No certification write after failed API verification.'})
  save_json(folder/'source_review.json',{'source_sha256':digest(code),'all_operations':operations,'review':'Dashboard, prerequisite status and all published apprentice-specific read alternatives have the same owner guard. API has no authentication switch. Sole certification creator also guards owner and writes placeholder apprentice/program 0. Task literals alone supplied all API identifiers.'})
  finding={**task,'state':'reject','layer1':'reject','layer2':'not_run_due_to_confirmed_environment_defect','reasons':['reproduced_target_owner_guard_hides_required_data'],'evidence_dir':str(folder),'confirmed':True,'explanation':'Apprentice 552 belongs to user 2, API fixes user 1. Two fresh real MCP runs return zero/false although independent DB facts are nonzero and certification prerequisites are met. No available authorized API path can read required target data or issue the requested target certification.'}
  save_json(folder/'finding.json',finding);findings=[x for x in findings if x['ledger_id']!=task['ledger_id']]+[finding]
 write_rows(run/'confirmed_defects.jsonl',findings)
 print('Local dependency defects confirmed:',[x['ledger_id'] for x in findings],flush=True)
 # Resolve the model review's contradictory adapter boolean with exact native input equality.
 root=run/'tasks/row154:task9';result=read_json(root/'result.json');base=root/'explicit_output_v2';c=next(c for c in result['judged_cases'] if c['name']=='empty_answer');p=Path(c['path'])/'judge/0'
 official=read_json(p/'official_request.json');wire=read_json(p/'request.json');normalized=copy.deepcopy(official);normalized['max_tokens']=normalized.pop('max_completion_tokens');assert normalized==wire
 payload=json.loads(official['messages'][1]['content'].split('\n\nverification_json:\n')[0].removeprefix('task_json:\n'))
 trace=read_json(Path(c['path'])/'trajectory.json');positive=read_json(base/'positive/trajectory.json');assert payload['user_task']==result['task'] and payload['trajectory']==trace['trajectory']
 assert trace['trajectory'][:-1]==positive['trajectory'][:-1] and trace['trajectory'][-1]['is_final'] and trace['trajectory'][-1]['content']=='{}'
 assert read_json(base/'reference.json')['expected']==json.loads(positive['final_answer']) and json.loads(positive['final_answer'])['applications']
 assert c['slots'][0]['valid'] and c['slots'][0]['accepted'] and not c['expected']
 audit={'confirmed_misclassification':True,'adapter_correct':True,'label_correct':True,'ambiguities':[],'label_errors':[],'method':'Deterministic root audit against actual official request, wire request, frozen native trajectory, complete DB reference and two positive executions. Original model dispute preserved; its adapter_correct=false contradicts its own rationale. No judge resampling.','checks':{'exact_native_trajectory':True,'final_answer_empty':True,'task_explicitly_requires_returned_applicant_ids':True,'same_actual_readonly_evidence':True,'wire_only_parameter_alias':True},'official_request_sha256':digest(p/'official_request.json'),'reference_sha256':digest(base/'reference.json')}
 save_json(base/'root_dispute.json',audit)
 if result['state']!='reject':save_json(root/'before_root_input_audit.json',result)
 c['root_dispute']=audit;result.update(state='reject',layer2='judge_defect',reasons=['confirmed_explicit_answer_false_accept'],early_stop='not_run_due_to_confirmed_defect')
 save_json(base/'result.json',result);save_json(root/'result.json',result)
 print('Explicit-output false acceptance independently confirmed.',flush=True)

if __name__=='__main__':asyncio.run(main())
