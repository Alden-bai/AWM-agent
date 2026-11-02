from __future__ import annotations
import asyncio, copy, hashlib, json, os, re, sqlite3, sys, time
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'evidence/legacy/source'))
sys.path.insert(0,str(ROOT/'agent-world-model'))
from awm_audit.common import backup, save_json, read_json, digest, now
from awm_audit.config import load_config
from awm_audit import runtime


def write_rows(path, rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r,ensure_ascii=False,default=str)+'\n' for r in rows))


def full_state(path):
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return {t[0]:[dict(x) for x in db.execute('SELECT * FROM "'+t[0].replace('"','""')+'"')]
                for t in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}


def resolve(value, work=None, results=None):
    results=results or []
    if isinstance(value,list):return [resolve(x,work,results) for x in value]
    if not isinstance(value,dict):return value
    if '$ref' in value:
        cur=results[value['$ref']]
        for key in value.get('path',[]):cur=cur[key]
        if 'where' in value:
            wanted=resolve(value['where'],work,results)
            cur=[r for r in cur if all(r.get(k)==v for k,v in wanted.items())]
        if value.get('take')=='one':
            if len(cur)!=1:raise ValueError(f'Expected exactly one public result, got {len(cur)}')
            cur=cur[0]
        if 'field' in value:
            cur=cur[value['field']] if isinstance(cur,dict) else [r[value['field']] for r in cur]
        return cur
    if '$calc' in value:
        vals=[resolve(x,work,results) for x in value['values']]
        a,b=vals
        return {'add':lambda:a+b,'subtract':lambda:a-b,'multiply':lambda:a*b,'divide':lambda:a/b}[value['$calc']]()
    if '$format' in value:return value['$format'].format(*[resolve(x,work,results) for x in value['values']])
    if any(str(k).startswith('$') for k in value):raise ValueError('Unregistered computation or private DB reference')
    return {k:resolve(v,work,results) for k,v in value.items()}


def query_check(sql, initial, final, trace):
    if not isinstance(sql,str) or not re.match(r'^\s*(SELECT|WITH)\b',sql,re.I):
        return {'passed':False,'error':'Only read-only SELECT/WITH allowed'}
    try:
        with sqlite3.connect(':memory:',uri=True) as db:
            with sqlite3.connect(f'file:{Path(final).resolve()}?mode=ro',uri=True) as src:src.backup(db)
            db.execute('ATTACH DATABASE ? AS initial',(f'file:{Path(initial).resolve()}?mode=ro',))
            db.execute("ATTACH DATABASE ':memory:' AS evidence")
            db.execute('CREATE TABLE evidence.answers(value TEXT)')
            db.execute('INSERT INTO evidence.answers VALUES (?)',(trace.get('final_answer') or '',))
            db.execute('CREATE TABLE evidence.tool_results(step INTEGER,name TEXT,value TEXT)')
            idx=0
            for row in trace.get('trajectory',[]):
                for tc in row.get('tool_calls',[]):
                    if tc['name']=='call_tool':
                        db.execute('INSERT INTO evidence.tool_results VALUES (?,?,?)',(idx,tc['arguments']['tool_name'],row['tool_response']['content']));idx+=1
            db.commit(); db.execute('PRAGMA query_only=ON')
            db.set_authorizer(lambda op,*args: sqlite3.SQLITE_DENY if op in (sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_PRAGMA) else sqlite3.SQLITE_OK)
            deadline=time.monotonic()+5
            db.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
            rows=db.execute(sql).fetchall()
            valid=len(rows)==1 and len(rows[0])==1 and rows[0][0] in (0,1)
            return {'passed':bool(valid and rows[0][0]==1),'rows':rows,'valid':valid}
    except Exception as e:return {'passed':False,'valid':False,'error':str(e)}


def evaluate(requirements, initial, final, trace):
    return {r['id']:{'description':r.get('description'),'sql':r.get('sql'),**query_check(r.get('sql'),initial,final,trace)} for r in requirements}


def flatten(value,path=()):
    if isinstance(value,dict):
        if any(k in value for k in ('$ref','$calc','$format')):return
        for k,v in value.items():yield from flatten(v,path+(k,))
    elif isinstance(value,list):
        for i,v in enumerate(value):yield from flatten(v,path+(i,))
    else:yield path,value


def validate_config(spec,task,contracts):
    issues=[];requirements=spec.get('requirements',[])
    if not requirements:issues.append('no_requirements')
    ids=[r.get('id') for r in requirements]
    if len(ids)!=len(set(ids)):issues.append('duplicate_requirement_ids')
    for r in requirements:
        if not r.get('source_quote') or r['source_quote'] not in task:issues.append('invalid_source_quote:'+str(r.get('id')))
        if not r.get('sql'):issues.append('missing_sql:'+str(r.get('id')))
    for c in spec.get('clauses',[]):
        if not c.get('quote') or c['quote'] not in task:issues.append('invalid_clause_quote')
        if any(i not in ids for i in c.get('requirements',[])):issues.append('unknown_clause_requirement')
    sources={(s['step'],tuple(s['path'])):s for s in spec.get('argument_sources',[])}
    for i,call in enumerate(spec.get('positive',{}).get('calls',[])):
        for path,value in flatten(call['arguments']):
            source=sources.get((i,path))
            if source is None:issues.append(f'missing_argument_source:{i}:{path}');continue
            kind=source.get('kind');quote=source.get('quote','')
            if kind=='task_literal' and (not quote or quote not in task):issues.append(f'invalid_literal_source:{i}:{path}')
            elif kind=='public_contract' and (not quote or quote not in contracts):issues.append(f'invalid_public_source:{i}:{path}')
            elif kind=='implementation_choice':
                if any('id'==str(k).lower() or str(k).lower().endswith('_id') for k in path):issues.append(f'private_id_choice:{i}:{path}')
                if not source.get('reason'):issues.append(f'unjustified_choice:{i}:{path}')
            elif kind not in ('task_literal','public_contract'):issues.append(f'unknown_source:{i}:{path}')
    checks={x['step'] for x in spec.get('tool_checks',[])}
    if checks!=set(range(len(spec.get('positive',{}).get('calls',[])))):issues.append('tool_checks_not_every_step')
    targets={x.get('target') for x in spec.get('negatives',[])}
    issues.extend('missing_negative:'+str(i) for i in ids if i not in targets)
    if not spec.get('boundaries'):issues.append('missing_boundary_review')
    for b in spec.get('boundaries',[]):
        if b.get('status') not in ('covered','not_applicable_with_evidence'):issues.append('boundary_missing:'+b.get('id',''))
        if b.get('status')=='not_applicable_with_evidence' and not b.get('evidence'):issues.append('unsupported_not_applicable')
    if spec.get('ambiguities'):issues.append('semantic_ambiguity')
    return sorted(set(issues))


def make_cases(spec):
    pos=copy.deepcopy(spec['positive']);pos.update(name='positive',expected=True,target=None)
    cases=[pos]
    if spec.get('equivalent_positive'):
        c=copy.deepcopy(spec['equivalent_positive']);c.setdefault('calls',copy.deepcopy(pos['calls']));c.update(name='equivalent_positive',expected=True,target=None);cases.append(c)
    for neg in spec.get('negatives',[]):
        c=copy.deepcopy(pos);c.update(name=neg['id'],expected=False,target=neg['target'],intervention=neg)
        if neg['kind']=='mutation':
            obj=c['calls'][neg['step']]['arguments'];path=neg['path']
            for k in path[:-1]:obj=obj[k]
            obj[path[-1]]=neg['value']
        elif neg['kind']=='omission':
            dropped=neg['step'];c['calls'].pop(dropped)
            # Adjust result references after omission; references TO removed call cannot be fabricated.
            def adjust(v):
                if isinstance(v,dict):
                    if '$ref' in v:
                        if v['$ref']==dropped:raise ValueError('Omitted call has downstream dependency; choose another negative')
                        if v['$ref']>dropped:v['$ref']-=1
                    for x in v.values():adjust(x)
                elif isinstance(v,list):
                    for x in v:adjust(x)
            adjust(c['calls']);adjust(c['answer'])
        elif neg['kind']=='answer':c['answer']=neg['value']
        elif neg['kind']=='no_action':c['calls']=[];c['answer']=neg.get('value','Done.')
        else:raise ValueError('Unsupported negative template')
        cases.append(c)
    if len({c['name'] for c in cases})!=len(cases):raise ValueError('Duplicate case IDs')
    for c in cases:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',c['name']):raise ValueError('Unsafe case ID')
    return cases


def configure_runtime(run):
    assets=read_json(Path(run)/'selected_assets.json')
    def scenario_asset(_run,name,scenario):
        matches=[r for r in assets[name] if r['scenario']==scenario]
        if len(matches)!=1:raise ValueError('Ambiguous environment asset')
        return matches[0]
    runtime.AWM=ROOT/'agent-world-model';runtime.scenario_asset=scenario_asset;runtime.resolve=resolve
    return assets


class Budget:
    def __init__(self,run):
        self.run=Path(run);self.path=self.run/'budget.sqlite';self.manifest=read_json(self.run/'manifest.json')
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, key TEXT UNIQUE, stage TEXT, reserved INTEGER, status TEXT, usage TEXT, error TEXT, started TEXT, finished TEXT)')
    def reserve(self,key,stage,max_tokens,messages):
        upper=len(json.dumps(messages,ensure_ascii=False).encode())+max_tokens+1000
        caps=read_json(self.run/'manifest.json')['budget']
        with sqlite3.connect(self.path,timeout=30,isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM requests WHERE key=?',(key,)).fetchone():raise RuntimeError('Request already reserved; interrupted requests never resampled')
            count,total=db.execute('SELECT count(*),coalesce(sum(reserved),0) FROM requests').fetchone()
            nstage=db.execute('SELECT count(*) FROM requests WHERE stage=?',(stage,)).fetchone()[0]
            if count>=caps['max_physical_requests'] or total+upper+70>caps['max_total_tokens'] or nstage>=caps['stage_requests'][stage]:
                db.execute('ROLLBACK');raise RuntimeError('budget_exhausted')
            c=db.execute('INSERT INTO requests(key,stage,reserved,status,started) VALUES (?,?,?,?,?)',(key,stage,upper,'started',now()));db.execute('COMMIT');return c.lastrowid
    def finish(self,rid,usage=None,error=None):
        with sqlite3.connect(self.path,timeout=30) as db:
            # Unknown transport usage conservatively retains reservation.
            if usage:db.execute('UPDATE requests SET reserved=?,status=?,usage=?,finished=? WHERE id=?',(usage.get('total_tokens',0),'response',json.dumps(usage),now(),rid))
            else:db.execute('UPDATE requests SET status=?,error=?,finished=? WHERE id=?',('transport_error',error,now(),rid))


class Model:
    def __init__(self,run):
        self.run=Path(run);self.budget=Budget(run);self.config=load_config('/home/bjx/app-agent/.env')
    async def request(self,folder,stage,kwargs):
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
        responsepath=folder/'response.json'
        if responsepath.exists():
            from openai.types.chat import ChatCompletion
            return ChatCompletion.model_validate(read_json(responsepath))
        retry_receipt=self.run/'resume_authorization_1.json'
        budget_key=str(folder.resolve())
        with sqlite3.connect(self.budget.path,timeout=30) as db:
            previous=db.execute('SELECT status,error FROM requests WHERE key=?',(budget_key,)).fetchone()
        if previous and retry_receipt.exists() and (previous[0]=='rejected_authorization' or previous[0]=='interrupted_unknown' and stage in ('build','extract','review')):
            retry_folder=folder/'transport_resume_1'
            if (retry_folder/'response.json').exists():
                from openai.types.chat import ChatCompletion
                data=read_json(retry_folder/'response.json');save_json(responsepath,data);return ChatCompletion.model_validate(data)
            save_json(folder/'resume_link.json',{'authorization':str(retry_receipt),'original_status':previous[0],'attempt_directory':str(retry_folder),'no_valid_response_resampled':True})
            budget_key=str(retry_folder.resolve())
        else:retry_folder=None
        if (self.run/'api_circuit.json').exists():raise RuntimeError('api_authorization_circuit_open; user action required')
        public={k:v for k,v in kwargs.items()}
        save_json(folder/'official_request.json',public)
        # Parameter alias only: preserve 4096 cap for the configured compatible endpoint.
        wire=copy.deepcopy(kwargs)
        if self.config.base_url.rstrip('/')=='https://api.deepseek.com' and 'max_completion_tokens' in wire:
            wire['max_tokens']=wire.pop('max_completion_tokens')
        save_json(folder/'request.json',wire)
        rid=self.budget.reserve(budget_key,stage,wire.get('max_tokens',wire.get('max_completion_tokens',0)),wire['messages'])
        from openai import AsyncOpenAI
        async with AsyncOpenAI(base_url=self.config.base_url,api_key=self.config.api_key,timeout=180,max_retries=0) as client:
            try:
                response=await client.chat.completions.create(**wire)
                data=response.model_dump(mode='json');save_json(responsepath,data)
                if retry_folder:save_json(retry_folder/'response.json',data)
                self.budget.finish(rid,data.get('usage'));return response
            except Exception as e:
                error=self.config.redact(e);save_json((retry_folder or folder)/'error.json',{'type':type(e).__name__,'error':error,'status_code':getattr(e,'status_code',None)})
                if getattr(e,'status_code',None) in (401,402,403):save_json(self.run/'api_circuit.json',{'status_code':e.status_code,'at':now(),'request_id':rid})
                self.budget.finish(rid,error=error)
                if getattr(e,'status_code',None) in (401,402,403):
                    with sqlite3.connect(self.budget.path,timeout=30) as db:db.execute('UPDATE requests SET reserved=0,status=? WHERE id=?',('rejected_authorization',rid))
                raise
    async def json(self,folder,stage,prompt,payload,max_tokens=16000):
        kwargs={'model':self.config.model,'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],'temperature':0,'max_tokens':max_tokens,'response_format':{'type':'json_object'},'extra_body':{'thinking':{'type':'disabled'}}}
        response=await self.request(folder,stage,kwargs)
        if response.choices[0].finish_reason!='stop':raise ValueError('invalid_model_finish:'+str(response.choices[0].finish_reason))
        value=json.loads(response.choices[0].message.content)
        if not isinstance(value,dict):raise ValueError('Expected JSON object')
        save_json(Path(folder)/'parsed.json',value);return value
    async def judge(self,folder,task,extraction,trajectory):
        from awm.core import verify
        # Each task runs in a separate process, so interception is not shared across tasks.
        wrapper=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:self.request(folder,'judge',kw))))
        original=verify.AsyncOpenAI;verify.AsyncOpenAI=lambda **kw:wrapper
        try:classification,details=await verify.run_llm_judge(task,extraction,self.config.base_url,self.config.api_key,self.config.model,trajectory)
        finally:verify.AsyncOpenAI=original
        result={'classification':classification,'details':details,'valid':False,'accepted':None}
        if (Path(folder)/'response.json').exists():
            data=read_json(Path(folder)/'response.json');choice=data['choices'][0]
            try:
                raw=json.loads(choice['message'].get('content') or '')
                result['valid']=choice['finish_reason']=='stop' and raw.get('classification') in ('complete','incomplete','agent_error') and classification==raw['classification']
            except (ValueError,TypeError,AttributeError):pass
            result.update(finish_reason=choice['finish_reason'],usage=data.get('usage'))
        if result['valid']:result['accepted']=classification=='complete'
        save_json(Path(folder)/'result.json',result);return result
