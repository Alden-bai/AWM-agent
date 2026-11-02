"""One bounded, explicitly prepared source-feedback repair pass; no retry loop."""
import asyncio,json,os,sqlite3
from pathlib import Path
from rubric_lean import ROOT,BASE,RUN,read_json,write_json,file_hash,load_config,validate_config,APIConfig,Budget,api_request,estimate_tokens,expand,unique_pairs
PROMPT=ROOT/'configs/rubric/repair_lean_v1.txt'
async def run():
    m=read_json(RUN/'manifest.json');cfg=load_config('/home/bjx/app-agent/.env');validate_config(cfg)
    assert (cfg.base_url,cfg.model)==(m['base_url'],m['model'])
    os.environ['RUBRIC_LEAN_REPAIR_KEY']=cfg.api_key
    api=APIConfig(base_url=cfg.base_url,model=cfg.model,api_key_env='RUBRIC_LEAN_REPAIR_KEY',timeout=240,temperature=0)
    b=Budget(BASE/'budget.sqlite',92,800000);prompt=PROMPT.read_text();destroot=RUN/'repair1'
    if (destroot/'prompt.txt').exists():assert (destroot/'prompt.txt').read_text()==prompt
    (destroot/'prompt.txt').write_text(prompt)
    tasks=[]
    for t in m['tasks']:
        d=destroot/t['task_id'].replace(':','__');payload=read_json(d/'input.json')
        assert file_hash(d/'input.json')==read_json(d/'origin.json')['repair_input_sha256']
        assert file_hash(Path(t['original_ref']))==t['original_sha256']
        msg=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
        tasks.append((estimate_tokens(msg),t,d,msg))
    write_json(destroot/'manifest.json',{'method':'fresh short reconstruction with task-specific source-audit feedback; not autonomous one-pass quality','max_requests':92,'max_total_tokens':800000,'max_output_tokens':1600,'budget_before':b.summary(),'tasks':[t['task_id'] for _,t,_,_ in tasks],'prompt_sha256':file_hash(PROMPT),'automatic_retries':0})
    for _,t,d,msg in sorted(tasks,key=lambda x:x[0],reverse=True):
        if (d/'response.json').exists():continue
        key='lean_repair1/'+t['task_id']
        with sqlite3.connect(BASE/'budget.sqlite') as db:
            if db.execute('select 1 from requests where request_key=?',(key,)).fetchone():raise RuntimeError('no_retry_previous_attempt')
        data=await api_request(api,dict(model=cfg.model,messages=msg,temperature=0,max_tokens=1600,response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}}),b,key,'rubric_lean_repair',d)
        print(json.dumps({'task_id':t['task_id'],'usage':data.get('usage'),'finish':data['choices'][0]['finish_reason']},ensure_ascii=False),flush=True)
    rows=[]
    for t in m['tasks']:
        d=destroot/t['task_id'].replace(':','__')
        try:
            data=read_json(d/'response.json');c=data['choices'][0];(d/'raw_content.txt').write_text(c['message']['content'] or '')
            if c['finish_reason']!='stop':raise ValueError('finish_reason='+str(c['finish_reason']))
            raw=json.loads(c['message']['content'],object_pairs_hook=unique_pairs)
            out=expand(raw,read_json(Path(t['original_ref'])),read_json(d/'provenance.json'))
            write_json(d/'compact_rubric.json',raw);write_json(d/'rubric.json',out)
            rows.append(dict(task_id=t['task_id'],items=len(out['rubric']),errors=[]))
        except Exception as e:rows.append(dict(task_id=t['task_id'],items=0,errors=[str(e)]))
    write_json(destroot/'validation.json',rows);print(json.dumps({'budget':b.summary(),'structure_pass':sum(not r['errors'] for r in rows)}))
if __name__=='__main__':asyncio.run(run())
