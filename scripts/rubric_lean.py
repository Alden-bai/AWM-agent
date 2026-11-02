"""Compact-source rubric pilot. Shares the original cumulative budget; no retries."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'evidence/legacy/source'))
from awm_audit.config import load_config,validate_config
from harness_awm.backend import Budget,api_request
from harness_awm.contracts import APIConfig
from harness_awm.io import write_json,read_json,file_hash
from harness_awm.protocol import unique_pairs,estimate_tokens
from rubric_pilot import validate_output
BASE=ROOT/'runs/rubric_pilot10_20261024'
RUN=ROOT/'runs/rubric_lean10_20261024'
PROMPT=ROOT/'configs/rubric/generate_lean_v1.txt'
MAX_OUTPUT=2200
MAX_REQUESTS=82  # prior 72 plus this authorized ten-task rerun; token ceiling unchanged


def compact(inp):
    facts=[];provenance={}
    def add(label,raw_quote,source):
        assert raw_quote in source['text'], (source['id'],raw_quote)
        n=len(facts)
        facts.append([n,label,raw_quote])
        provenance[str(n)]={'source_id':source['id'],'quote':raw_quote}
    for source in inp['sources']:
        kind=source['kind']
        if kind=='task':
            add('task',source['text'],source)
        elif kind=='schema':
            # Keep constraints/FKs and nullable fields; strip only presentation whitespace on wire.
            n=len(facts);facts.append([n,source['id'],' '.join(source['text'].split())])
            provenance[str(n)]={'source_id':source['id'],'quote':source['text']}
        elif kind=='public_contract':
            c=json.loads(source['text']);op=c['operation_id']
            def description(label,value):
                if value:
                    # Original sources contain JSON text: preserve exact escaped substring.
                    quote=json.dumps(value,ensure_ascii=False)[1:-1]
                    add(label,quote,source)
            description(op,c.get('description') or c.get('summary'))
            for name,p in c.get('request_params',{}).items():
                description(op+'.'+name+('!' if p.get('required') else '?'),p.get('description'))
            def walk(node,path):
                if not isinstance(node,dict):return
                # Keep meaningful response descriptions, but generic field labels are already in schema.
                desc=node.get('description','')
                if isinstance(desc,str) and desc and (len(desc.split())>5 or any(w in desc.lower() for w in ['current','implicit','default','inclusive','exclusive','aggregat','total','average','group'])):
                    description(op+' -> '+path,desc)
                for name,value in node.items():
                    if name != 'example' and isinstance(value,dict):walk(value,path+'.'+name if path else name)
            walk(c.get('response',{}),'')
    return {'task_id':inp['task_id'],'facts':facts,'note':'!必需参数，?可选；事实摘录不含数据库值；未摘录接口不表示不存在；schema约束保留。'},provenance


def prepare():
    if (RUN/'manifest.json').exists():raise SystemExit('Already prepared; frozen inputs retained')
    m=read_json(BASE/'manifest.json');rows=[]
    prompt=PROMPT.read_text()
    for t in m['tasks']:
        p=BASE/t['input_ref'];assert file_hash(p)==t['input_sha256']
        packet,provenance=compact(read_json(p));dest=RUN/t['task_id'].replace(':','__')
        write_json(dest/'input.json',packet);write_json(dest/'provenance.json',provenance)
        messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(packet,ensure_ascii=False,separators=(',',':'))}]
        rows.append(dict(task_id=t['task_id'],original_ref=str(p),original_sha256=file_hash(p),packet_sha256=file_hash(dest/'input.json'),provenance_sha256=file_hash(dest/'provenance.json'),facts=len(packet['facts']),input_utf8_bytes=len(messages[1]['content'].encode()),reservation_upper=estimate_tokens(messages)+MAX_OUTPUT+512))
    RUN.mkdir(parents=True,exist_ok=True);(RUN/'prompt.txt').write_text(prompt)
    write_json(RUN/'manifest.json',dict(tasks=rows,base_url=m['base_url'],model=m['model'],max_output=MAX_OUTPUT,prompt_sha256=file_hash(PROMPT),budget_ledger=str(BASE/'budget.sqlite'),max_requests=MAX_REQUESTS,max_total_tokens=800000,automatic_retries=0,full_run=False,method='fresh compact inputs; no old rubrics or task-specific audit feedback',baseline_budget=Budget(BASE/'budget.sqlite',MAX_REQUESTS,800000).summary(),selection='same frozen purposeful ten tasks; tuning sample, not held-out'))
    write_json(RUN/'packet_stats.json',rows)
    print(json.dumps(rows,ensure_ascii=False))


def expand(raw,inp,provenance):
    if not isinstance(raw,dict) or not isinstance(raw.get('items'),list) or not raw['items']:raise ValueError('missing_items')
    out={'task_id':inp['task_id'],'rubric':[],'coverage':[],'unresolved':[]}
    for n,x in enumerate(raw['items'],1):
        for key in ('c','e','p','g','m'):
            if not isinstance(x.get(key),str) or (key!='g' and not x[key].strip()):raise ValueError('bad_item:'+str(n)+':'+key)
        if x['m'] not in ('d','s','h'):raise ValueError('bad_method')
        if not isinstance(x.get('s'),list) or not x['s'] or any(type(v)!=int or str(v) not in provenance for v in x['s']):raise ValueError('bad_source_ids')
        refs=[provenance[str(v)] for v in x['s']]
        prerequisite='所列证据已完整取得且无冲突'+('，并且 '+x['g'] if x['g'] else '')
        item={'id':f'R{n:02d}','criterion':x['c'],'sources':refs,'requirement_level':'required',
            'evidence':[{'description':x['e'],'source_id':None}],
            'decision':{'method':{'d':'deterministic','s':'semantic','h':'hybrid'}[x['m']],
                'satisfied_when':f'G：{prerequisite}。G成立，且P为真。P：{x["p"]}',
                'violated_when':f'G：{prerequisite}。G成立，且同一P为假。P：{x["p"]}',
                'unknown_when':f'G不成立（证据未完整取得、证据冲突或下述必要语义未确定）：{prerequisite}。完整工件本身缺少要求不属于证据缺失，应比较P。'}}
        out['rubric'].append(item)
    for u in raw.get('unresolved',[]):
        if not isinstance(u.get('issue'),str) or not isinstance(u.get('needed'),str) or not isinstance(u.get('items'),list) or any(type(n)!=int or not 1<=n<=len(out['rubric']) for n in u['items']):raise ValueError('bad_unresolved')
        out['unresolved'].append({'issue':u['issue'],'needed':u['needed'],'affected_items':[f'R{n:02d}' for n in u['items']]})
    # This records task linkage, not a claim of independently proven clause coverage.
    out['coverage']=[{'quote':inp['task'],'item_ids':[x['id'] for x in out['rubric']],'interpretation':'整题关联；逐义务覆盖须独立语义审查，不由该关联证明。'}]
    errors=validate_output(out,inp)
    if errors:raise ValueError(str(errors))
    return out


async def run():
    m=read_json(RUN/'manifest.json');assert file_hash(PROMPT)==m['prompt_sha256']
    cfg=load_config('/home/bjx/app-agent/.env');validate_config(cfg)
    assert (cfg.base_url,cfg.model)==(m['base_url'],m['model'])
    os.environ['RUBRIC_LEAN_API_KEY']=cfg.api_key
    api=APIConfig(base_url=cfg.base_url,model=cfg.model,api_key_env='RUBRIC_LEAN_API_KEY',timeout=240,temperature=0)
    budget=Budget(m['budget_ledger'],m['max_requests'],m['max_total_tokens'])
    for t in sorted(m['tasks'],key=lambda t:t['reservation_upper'],reverse=True):
        dest=RUN/t['task_id'].replace(':','__');key='lean_v1/'+t['task_id']
        if (dest/'response.json').exists():continue
        with sqlite3.connect(m['budget_ledger']) as db:
            if db.execute('SELECT 1 FROM requests WHERE request_key=?',(key,)).fetchone():raise RuntimeError('prior_request_reserved_no_retry')
        assert file_hash(Path(t['original_ref']))==t['original_sha256']
        assert file_hash(dest/'input.json')==t['packet_sha256']
        assert file_hash(dest/'provenance.json')==t['provenance_sha256']
        messages=[{'role':'system','content':(RUN/'prompt.txt').read_text()},{'role':'user','content':json.dumps(read_json(dest/'input.json'),ensure_ascii=False,separators=(',',':'))}]
        data=await api_request(api,dict(model=cfg.model,messages=messages,temperature=0,max_tokens=m['max_output'],response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}}),budget,key,'rubric_lean',dest)
        print(json.dumps({'task_id':t['task_id'],'usage':data.get('usage'),'finish':data['choices'][0]['finish_reason']},ensure_ascii=False),flush=True)
    validate()


def validate():
    m=read_json(RUN/'manifest.json');rows=[]
    for t in m['tasks']:
        d=RUN/t['task_id'].replace(':','__')
        try:
            data=read_json(d/'response.json');choice=data['choices'][0]
            (d/'raw_content.txt').write_text(choice['message']['content'] or '')
            if choice['finish_reason']!='stop':raise ValueError('finish_reason='+str(choice['finish_reason']))
            raw=json.loads(choice['message']['content'],object_pairs_hook=unique_pairs)
            out=expand(raw,read_json(Path(t['original_ref'])),read_json(d/'provenance.json'))
            write_json(d/'compact_rubric.json',raw);write_json(d/'rubric.json',out)
            rows.append(dict(task_id=t['task_id'],items=len(out['rubric']),errors=[],usage=data.get('usage')))
        except Exception as e:rows.append(dict(task_id=t['task_id'],items=0,errors=[type(e).__name__+':'+str(e)]))
    write_json(RUN/'validation.json',rows)
    print(json.dumps({'structure_pass':sum(not x['errors'] for x in rows),'tasks':len(rows),'budget':Budget(m['budget_ledger'],m['max_requests'],m['max_total_tokens']).summary()},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run','validate']);a=p.parse_args()
    if a.action=='run':asyncio.run(run())
    elif a.action=='prepare':prepare()
    else:validate()
