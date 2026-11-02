"""Apply one planned item-level LLM patch, retaining immutable originals."""
import asyncio,json,os,sqlite3
from pathlib import Path
from rubric_lean import ROOT,BASE,RUN,read_json,write_json,file_hash,load_config,validate_config,APIConfig,Budget,api_request,estimate_tokens,expand,unique_pairs
async def run():
 m=read_json(RUN/'manifest.json');cfg=load_config('/home/bjx/app-agent/.env');validate_config(cfg);assert(cfg.base_url,cfg.model)==(m['base_url'],m['model'])
 os.environ['RUBRIC_LEAN_PATCH_KEY']=cfg.api_key
 api=APIConfig(base_url=cfg.base_url,model=cfg.model,api_key_env='RUBRIC_LEAN_PATCH_KEY',timeout=240,temperature=0);b=Budget(BASE/'budget.sqlite',98,800000)
 prompt=(ROOT/'configs/rubric/patch_lean_v1.txt').read_text();folder=RUN/'patch1';(folder/'prompt.txt').write_text(prompt);plan=read_json(folder/'plan.json');tasks=[]
 for t in plan:
  d=folder/t['task_id'].replace(':','__');assert file_hash(d/'input.json')==t['input_sha256'];assert file_hash(RUN/'repair1'/d.name/'compact_rubric.json')==t['base_sha256']
  msg=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(read_json(d/'input.json'),ensure_ascii=False,separators=(',',':'))}];tasks.append((estimate_tokens(msg),t,d,msg))
 write_json(folder/'manifest.json',{'method':'six-task item-level source-feedback patches; not autonomous one-pass generation','max_requests':98,'max_total_tokens':800000,'max_output_tokens':1200,'budget_before':b.summary(),'automatic_retries':0})
 for _,t,d,msg in sorted(tasks,key=lambda x:x[0],reverse=True):
  if (d/'response.json').exists():continue
  key='lean_patch1/'+t['task_id']
  with sqlite3.connect(BASE/'budget.sqlite') as db:
   if db.execute('select 1 from requests where request_key=?',(key,)).fetchone():raise RuntimeError('no_retry')
  data=await api_request(api,dict(model=cfg.model,messages=msg,temperature=0,max_tokens=1200,response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}}),b,key,'rubric_lean_patch',d)
  print(json.dumps({'task_id':t['task_id'],'usage':data.get('usage'),'finish':data['choices'][0]['finish_reason']},ensure_ascii=False),flush=True)
 assemble()

def assemble():
 m=read_json(RUN/'manifest.json');folder=RUN/'patch1';plan=read_json(folder/'plan.json')
 rows=[];byid={x['task_id']:x for x in plan}
 for t in m['tasks']:
  key=t['task_id'];name=key.replace(':','__');base=RUN/'repair1'/name;raw=read_json(base/'compact_rubric.json');dest=RUN/'final'/name
  try:
   if key in byid:
    data=read_json(folder/name/'response.json');c=data['choices'][0]
    if c['finish_reason']!='stop':raise ValueError('truncated_patch')
    patches=json.loads(c['message']['content'],object_pairs_hook=unique_pairs);allowed=set(byid[key]['allowed_ids']);seen=set();deletes=set(patches.get('delete',[]))
    if not deletes<=allowed:raise ValueError('unplanned_delete')
    for p in patches['patches']:
     n=p['id']
     if n not in allowed or n in seen or n in deletes or set(p)-{'id','c','e','p','g'}:raise ValueError('invalid_patch')
     seen.add(n)
     for k,v in p.items():
      if k!='id':
       if not isinstance(v,str):raise ValueError('bad_patch_value')
       raw['items'][n-1][k]=v
    old_to_new={n:i+1 for i,n in enumerate(n for n in range(1,len(raw['items'])+1) if n not in deletes)}
    raw['items']=[x for n,x in enumerate(raw['items'],1) if n not in deletes]
    for u in raw['unresolved']:u['items']=[old_to_new[n] for n in u['items'] if n in old_to_new]
    raw['unresolved']=[u for u in raw['unresolved'] if u['items']]
   out=expand(raw,read_json(Path(t['original_ref'])),read_json(base/'provenance.json'))
   write_json(dest/'compact_rubric.json',raw);write_json(dest/'rubric.json',out);write_json(dest/'origin.json',{'base':str(base),'patch':str(folder/name) if key in byid else None,'raw_model_outputs_preserved':True})
   rows.append({'task_id':key,'items':len(raw['items']),'errors':[]})
  except Exception as e:
   # Reject the entire invalid patch; keep the prior valid artifact without changing model text.
   raw=read_json(base/'compact_rubric.json')
   out=expand(raw,read_json(Path(t['original_ref'])),read_json(base/'provenance.json'))
   write_json(dest/'compact_rubric.json',raw);write_json(dest/'rubric.json',out)
   write_json(dest/'origin.json',{'base':str(base),'patch_rejected':str(folder/name),'reason':str(e),'raw_model_outputs_preserved':True})
   rows.append({'task_id':key,'items':len(raw['items']),'errors':[],'rejected_patch_errors':[str(e)]})
 write_json(RUN/'final/validation.json',rows);print(json.dumps({'structure_pass':sum(not r['errors'] for r in rows),'rejected_patches':sum(bool(r.get('rejected_patch_errors')) for r in rows)}))
if __name__=='__main__':
 import sys
 if len(sys.argv)>1 and sys.argv[1]=='assemble':assemble()
 else:asyncio.run(run())
