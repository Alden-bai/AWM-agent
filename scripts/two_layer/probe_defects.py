"""Reproduce a scoped environment defect without model judgments or DB-derived inputs."""
import asyncio, ast, json, sys
from pathlib import Path
from run import *

async def main():
 run=ROOT/'cleaning_runs/two_layer_pilot100_v1';m=read_json(run/'manifest.json');assets=configure_runtime(run)
 scenario='education_training_management_1';code=next(x['full_code'] for x in assets['gen_envs.jsonl'] if x['scenario']==scenario)
 operations={n.name:ast.get_source_segment(code,n) for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
 specs={2:('create_competency_record',{'apprentice_id':552,'body':{'competency_id':205,'record_date':'2025-01-10','rating':4,'comments_short':'Completed full install with minimal guidance'}},'competency_records'),3:('create_work_log_entry',{'apprentice_id':552,'body':{'work_date':'2025-01-12','job_type':'Residential wiring rough-in','location':'123 Oak St, Springfield','hours_worked':7.5,'supervisor_signature_flag':1}},'work_log_entries'),6:('create_class_evaluation',{'class_id':88,'body':{'apprentice_id':552,'score':86,'pass_fail':'pass','evaluation_date':'2025-02-06','comments':'Good safety awareness and tool handling'}},'class_evaluations')}
 contract=next(r['api_spec'] for r in assets['gen_spec.jsonl'] if r['scenario']==scenario)
 findings=[];initial=run/'environments'/scenario/'initial.db'
 for idx,(tool,args,table) in specs.items():
  task=next(t for t in m['tasks'] if t['scenario']==scenario and t['task_idx']==idx);folder=run/'confirmed_environment_defects'/task['ledger_id'];folder=folder/'integer_boolean_v2' if idx==3 else folder;folder.mkdir(parents=True,exist_ok=True)
  traces=[]
  for repeat in range(2):
   f=folder/str(repeat)
   trace=read_json(f/'trajectory.json') if (f/'trajectory.json').exists() else await runtime.execute_case(run,task,{'name':'valid_target_probe','calls':[{'tool':tool,'arguments':args}],'answer':'','expected':True},initial,f)
   before=full_state(f/'initial.db');after=full_state(f/'final.db');delta=differences(before,after)
   new=delta.get(table,{}).get('added',[])
   ev={'failure':trace['failure'],'new_rows':new,'target_new_rows':[r for r in new if r['apprentice_id']==552],'wrong_placeholder_rows':[r for r in new if r['apprentice_id']==0],'target_apprentice':[r for r in before['apprentices'] if r['id']==552],'trajectory':trace['trajectory'],'database_foreign_key_check':None}
   with sqlite3.connect(f/'final.db') as db:ev['database_foreign_key_check']=db.execute('PRAGMA foreign_key_check').fetchall()
   save_json(f/'independent_check.json',ev);traces.append(ev)
  assert all(not e['failure'] and e['wrong_placeholder_rows'] and not e['target_new_rows'] and e['target_apprentice'][0]['user_id']==2 for e in traces)
  # List ALL published writers of target table; inspect guard and absence of auth switching.
  candidates={name:src for name,src in operations.items() if table in src or {'competency_records':'CompetencyRecord(','work_log_entries':'WorkLogEntry(','class_evaluations':'ClassEvaluation('}[table] in src}
  save_json(folder/'source_review.json',{'affected_create':operations[tool],'related_operations':candidates,'all_operations':list(operations),'public_contract':contract,'source_sha256':digest(code)})
  finding={'ledger_id':task['ledger_id'],'scenario':scenario,'task_idx':idx,'state':'reject','layer1':'reject','layer2':'not_run_due_to_confirmed_environment_defect','reasons':['reproduced_target_owner_guard_writes_wrong_placeholder'],'evidence_dir':str(folder),'confirmed':True,'explanation':'Task literal apprentice 552 belongs to user 2. Published create endpoint is fixed to user 1, inserts a dummy record for apprentice 0 and does not create requested target record; reproduced on two fresh DBs. Public API has no authentication switch or alternative writer that creates requested record. This excludes current task/environment version, not every task in this environment.','provenance':'Every supplied identifier, date, score and content comes directly from the task; no hidden DB ID injected.'}
  save_json(folder/'finding.json',finding);findings.append(finding)
 write_rows(run/'confirmed_defects.jsonl',findings)
 print(json.dumps(findings,ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
