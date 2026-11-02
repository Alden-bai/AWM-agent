"""Offline v7 input-only plan. Does not import API clients or modify the ledger."""
import copy, hashlib, json, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'runs/rubric_pilot10_20261024'
OUT=ROOT/'runs/rubric_v7_input_only_plan_20261024'
def read(p):return json.loads(p.read_text())
def dump(p,x):
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def wire(x):return json.dumps(x,ensure_ascii=False,separators=(',',':'))
def digest(s):return hashlib.sha256(s.encode()).hexdigest()
def prune_examples(x):
 if isinstance(x,list):return [prune_examples(v) for v in x]
 if isinstance(x,dict):return {k:prune_examples(v) for k,v in x.items() if k!='example'}
 return x
def main():
 m=read(BASE/'manifest.json');rows=[]
 for t in m['tasks']:
  name=t['task_id'].replace(':','__');old=read(BASE/'v7'/name/'request.json');usage=read(BASE/'v7'/name/'response.json')['usage']
  payload=json.loads(old['messages'][1]['content']);new=copy.deepcopy(payload)
  removed=[]
  for a,b in zip(payload['sources'],new['sources']):
   if a['kind']=='public_contract':
    original=json.loads(a['text']);reduced=prune_examples(original)
    b['text']=json.dumps(reduced,ensure_ascii=False)
    assert json.loads(b['text'])==prune_examples(original)
    removed.append(a['id'])
   else:assert a==b
  task_source=next(s['text'] for s in new['sources'] if s['kind']=='task')
  if new.get('task')==task_source:del new['task']
  for key in ('previous_rubric_raw','source_review_feedback','repair_scope'):
   assert new[key]==payload[key]
  assert new['tool_inventory']==payload['tool_inventory']
  candidate=copy.deepcopy(old);candidate['messages'][1]['content']=wire(new)
  assert candidate['messages'][0]==old['messages'][0]
  assert {k:v for k,v in candidate.items() if k!='messages'}=={k:v for k,v in old.items() if k!='messages'}
  old_bytes=len(wire(old['messages']).encode());new_bytes=len(wire(candidate['messages']).encode())
  estimate=usage['prompt_tokens']*new_bytes/old_bytes
  row={'task_id':t['task_id'],'original_request_ref':str(BASE/'v7'/name/'request.json'),
       'old_message_utf8_bytes':old_bytes,'candidate_message_utf8_bytes':new_bytes,
       'observed_v7_prompt_tokens':usage['prompt_tokens'],'estimated_prompt_tokens':round(estimate),
       'observed_v7_completion_tokens':usage['completion_tokens'],
       'estimated_total_tokens':round(estimate)+usage['completion_tokens'],
       'reservation_upper_bytes_plus_output':new_bytes+128+512+old['max_tokens'],
       'prompt_sha256':digest(candidate['messages'][0]['content']),
       'all_nonexample_contract_attributes_preserved':True,
       'draft_and_feedback_unchanged':True}
  dump(OUT/'inputs'/name/'candidate_request.json',candidate);rows.append(row)
 with sqlite3.connect('file:'+str(BASE/'budget.sqlite')+'?mode=ro',uri=True) as db:
  attempts,total=db.execute('select count(*),sum(reserved) from requests').fetchone()
  unknown=db.execute("select sum(reserved) from requests where usage is null").fetchone()[0]
 estimate=sum(x['estimated_total_tokens'] for x in rows)
 summary={'status':'offline_plan_only_not_authorized_for_dispatch','tasks':10,
  'method':'same v7 system/output/model, same v6 drafts and v6 reviews; only remove endpoint example attributes and duplicate top-level task, compact outer JSON',
  'estimation_method':'per-task observed v7 prompt tokens times UTF-8 message byte ratio, plus unchanged observed v7 completion tokens; NOT exact tokenization',
  'estimated_generation_tokens':estimate,'old_v7_generation_tokens':110390,
  'estimated_total_saving_fraction':1-estimate/110390,
  'generation_planning_range':[90000,120000],'up_to_two_task_repairs_range':[10000,26000],
  'total_planning_range':[100000,146000],'proposed_experiment_cap':160000,
  'current_cumulative_accounted_tokens':total,'current_unknown_reserved_tokens':unknown,
  'current_cap':800000,'current_remaining':800000-total,
  'proposed_cumulative_cap_if_approved':total+160000,
  'cap_changed':False,'remote_requests_made':0,'historical_attempts':attempts,
  'quality_target':'at least 8/10 provisional source-reviewed drafts; no regression on previously accepted eight; not guaranteed',
  'review_cost_boundary':'local validation and Codex source review; separate automated LLM reviewer NOT included',
  'new_task_generalization':'not measured; this controlled replay cannot estimate fresh generation quality on 10000 tasks',
  'dispatch_policy':'single concurrency, larger reservations first, 10 initial requests maximum, at most two one-shot task repairs, no automatic retries',
  'rows':rows}
 dump(OUT/'estimate.json',summary)
 print(json.dumps({k:v for k,v in summary.items() if k!='rows'},ensure_ascii=False))
if __name__=='__main__':main()
