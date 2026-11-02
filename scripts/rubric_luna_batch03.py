"""Frozen batch03 preparation and bounded real-pipeline trial.

This entry intentionally exposes no unbounded production run. The authorized
trial is at most 180 seconds of dispatch and 13 calls, at initial concurrency 3.
Long-running automatic promotion and final bank publication require separate
implementation/validation; the short trial cannot qualify for any promotion.
"""
import argparse
import asyncio
import collections
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import tempfile
import time

import rubric_luna_batch02 as legacy
from rubric_luna_dispatch import DispatchPaused, Gate, Writer, finish_cleanup

ROOT = legacy.ROOT
RUN_ID = 'rubric_luna_high_3000_batch03'
RUN = ROOT/'runs'/RUN_ID
REGISTRY = ROOT/'runs/rubric_luna_registry'
STARTUP = ROOT/'configs/rubric/luna_high_3000/run_3000.txt'
HISTORY = [ROOT/'runs'/name for name in ('rubric_luna_high_100_20261024',
           'rubric_luna_high_1000_batch01', 'rubric_luna_high_2900_batch02')]
DATA = ROOT/'data/awm-dde80a0'
SIZE, SEED = 3000, 20261026
MODEL, EFFORT = legacy.MODEL, legacy.EFFORT
read, digest, now = legacy.read, legacy.digest, legacy.now


def fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(directory):
    directory = Path(directory)
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        fsync_dir(path.parent)


def atomic(path, content):
    path = Path(path)
    durable_mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def freeze(path, value, raw=False):
    path = Path(path)
    content = value if raw else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if path.exists():
        if path.read_text() != content:
            raise ValueError('frozen_file_changed:'+str(path))
    else:
        atomic(path, content)
    return path


def append(path, value):
    path = Path(path)
    durable_mkdir(path.parent)
    with path.open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(',', ':'))+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def configure():
    legacy.RUN, legacy.RUN_ID, legacy.REGISTRY = RUN, RUN_ID, REGISTRY
    legacy.STARTUP_PROMPT, legacy.SIZE, legacy.SEED = STARTUP, SIZE, SEED
    legacy.DATA = DATA
    legacy.atomic, legacy.freeze, legacy.snapshot = atomic, freeze, snapshot
    legacy.base.RUN = RUN
    legacy.base.freeze, legacy.base.snapshot = freeze, snapshot


def registry_event(reason, batches=(), tasks=()):
    """Append authoritative events only; project the full indices at trial end."""
    with legacy.locked(REGISTRY/'.lock'):
        old_b, old_t = legacy.replay()
        bs = [b for b in batches if old_b.get(b['run_id']) != b]
        ts = [t for t in tasks if old_t.get((t['dataset_id'], t['task_id'], t['run_id'])) != t]
        if not bs and not ts:
            return
        append(REGISTRY/'events.jsonl', dict(at=now(), reason=reason, batches=bs, tasks=ts))
        old_b.update({b['run_id']:b for b in bs})
        old_t.update({(t['dataset_id'],t['task_id'],t['run_id']):t for t in ts})
        stat = (REGISTRY/'events.jsonl').stat()
        legacy.REGISTRY_CACHE[str(REGISTRY/'events.jsonl')] = ((stat.st_size,stat.st_mtime_ns),old_b,old_t)


def historical_audit():
    """Read-only identity/accounting validation, without semantic re-review."""
    batches, registered = legacy.replay()
    legacy.verify_registered_batches(batches, registered)
    ids, total, unknown, frozen = set(), 0, 0, []
    hashes = None
    for directory in HISTORY:
        m, chain = legacy.execution_manifest(directory)
        current = {t['task_id'] for t in m['tasks']}
        assert not ids & current, 'historical_duplicate'
        ids |= current
        u = read(directory/'usage.json')
        assert legacy.usage_summary(u['threads'])['totals'] == u['totals']
        total += u['totals']['total_tokens']
        unknown += len(u['unknown_usage'])
        assert hashes is None or hashes == m['source_file_hashes']
        hashes = m['source_file_hashes']
        for name in ('generate','judge','revise','execute','id_rules'):
            assert digest(directory/'prompts'/(name+'.txt')) == digest(HISTORY[0]/'prompts'/(name+'.txt'))
        frozen.append(dict(run_id=directory.name, sample_size=m['sample_size'], chain=chain,
                      completion=read(directory/'completion.json') if (directory/'completion.json').exists() else None,
                      format_differences=[] if (directory/'completion.json').exists() else
                        ['completion.json absent in legacy format; results/retention/statuses and registry reconciled instead'],
                      record_hashes={name:digest(directory/name) for name in
                         ('results.json','retention_manifest.json','accepted_manifest.json','usage.json',
                          'scheduler_usage.json','completion.json','report_zh.md') if (directory/name).exists()}))
    assert len(ids) == 4000 and total == 404705636 and unknown == 3
    assert ids <= {t['task_id'] for t in registered.values()}
    return dict(history=frozen, ids=sorted(ids), known_tokens=total, unknown_calls=unknown,
                source_file_hashes=hashes, checked_at=now())


def input_row(task_id, assets):
    scenario, index = task_id.rsplit(':',1)
    index = int(index)
    text = assets['gen_tasks'][scenario][0]['tasks'][index]
    folder = RUN/'inputs'/task_id.replace(':','__')
    refs = {name:dict(path=str(freeze(RUN/'sources'/scenario/(name+'.json'),rows[scenario][1],raw=True)),
                      sha256=hashlib.sha256(rows[scenario][1].encode()).hexdigest())
            for name,rows in assets.items()}
    row = dict(task_id=task_id,scenario=scenario,index=index,raw_sources=refs,
               task_sha256=hashlib.sha256(text.encode()).hexdigest())
    try:
        endpoints = [e for group in assets['gen_spec'][scenario][0]['api_spec']['api_groups'] for e in group['endpoints']]
        sources = [dict(id='task',kind='task',text=text),
                   dict(id='contract:'+scenario,kind='public_contract',text=assets['gen_spec'][scenario][1])]
        for table in assets['gen_db'][scenario][0]['db_schema']['tables']:
            sources.append(dict(id='schema:'+table['name'],kind='schema',
                                text=table['ddl']+'\n'+'\n'.join(table.get('indexes',[]))))
        original = dict(task_id=task_id,task=text,sources=sources,
            tool_inventory=[dict(operation_id=e.get('operation_id'),method=e.get('method'),path=e.get('path')) for e in endpoints],
            available_evidence_types=['执行前后数据库快照（可按公开DDL查询）','工具参数、真实返回、事件顺序及时间','最终答复'],
            evidence_note='这里只给出将来可收集的证据类型，没有初态、时钟或真实执行值；动作中间状态不保证可重建。',quality_policy=None)
        freeze(folder/'original.json',original)
        view, audit = legacy.prepare_input(original)
        freeze(folder/'view.json',view)
        freeze(folder/'audit.json',audit)
        row['preparation_status'] = 'ready'
        for name in ('original','view','audit'):
            path = folder/(name+'.json')
            row[name+'_ref'],row[name+'_sha256'] = str(path),digest(path)
    except Exception as exc:
        path = freeze(folder/'retained_task.json',dict(task_id=task_id,task=text,raw_sources=refs))
        row.update(preparation_status='preparation_failed',error=repr(exc),retained_task_ref=str(path))
    return row


def prepare():
    configure()
    # Caller holds batch lock, then acquires registry lock: never the reverse.
    audit = historical_audit()
    with legacy.locked(REGISTRY/'.lock'):
        batches, registered = legacy.replay()
        if (RUN/'manifest.json').exists():
            m = read(RUN/'manifest.json')
            assert m['run_id'] == RUN_ID and m['sample_size'] == SIZE
            assert m['sampling']['seed'] == SEED
            assert digest(STARTUP) == m['prompt_hashes'][STARTUP.name]
            legacy.verify(m)
            assert set(t['task_id'] for t in m['tasks']) == {
                t['task_id'] for t in registered.values() if t['run_id']==RUN_ID}
            return m
        assets = legacy.load_assets()
        hashes = {name:digest(DATA/(name+'.jsonl')) for name in assets}
        assert hashes == audit['source_file_hashes'], 'dataset_changed'
        population = sorted(f'{s}:{i}' for s,(r,_) in assets['gen_tasks'].items() for i in range(len(r['tasks'])))
        assert len(population) == len(set(population)) == 10000
        excluded = sorted(set(audit['ids']) | {t['task_id'] for t in registered.values() if t['dataset_id']==legacy.DATASET_ID and t['run_id']!=RUN_ID})
        for t in registered.values():
            scenario,index = t['task_id'].rsplit(':',1)
            assert hashlib.sha256(assets['gen_tasks'][scenario][0]['tasks'][int(index)].encode()).hexdigest() == t['task_sha256']
        eligible = sorted(set(population)-set(excluded))
        if (RUN/'reservation_intent.json').exists():
            intent = read(RUN/'reservation_intent.json')
            assert intent['source_file_hashes'] == hashes and intent['seed'] == SEED
            assert intent['startup_prompt_sha256'] == digest(STARTUP)
            selection = intent['selection']
        else:
            assert len(eligible) >= SIZE
            selection = random.Random(SEED).sample(eligible,SIZE)
            intent = dict(run_id=RUN_ID,created_at=now(),selection=selection,seed=SEED,
                          source_file_hashes=hashes,startup_prompt_sha256=digest(STARTUP))
            freeze(RUN/'reservation_intent.json',intent)
        assert len(selection) == len(set(selection)) == SIZE
        assert not set(selection)&set(excluded)
        # The reservation event is committed BEFORE slow preparation. A crash
        # cannot expose these IDs as eligible to another registry reader.
        minimal = []
        for tid in selection:
            scenario,index = tid.rsplit(':',1)
            minimal.append(dict(dataset_id=legacy.DATASET_ID,task_id=tid,run_id=RUN_ID,
                scenario=scenario,index=int(index),task_sha256=hashlib.sha256(assets['gen_tasks'][scenario][0]['tasks'][int(index)].encode()).hexdigest(),
                status='reserved',model_call_occurred=False,manifest_pending=True,
                original_ref=str(RUN/'inputs'/tid.replace(':','__')/'original.json')))
        b = dict(run_id=RUN_ID,dataset_id=legacy.DATASET_ID,status='preparing',sample_size=SIZE,
                 seed=SEED,manifest_ref=str(RUN/'manifest.json'),reservation_intent_ref=str(RUN/'reservation_intent.json'),
                 source_file_hashes=hashes,model=MODEL,effort=EFFORT,luna_actual_usage=None,unknown_usage=[])
        legacy.commit('持久化完整3000题占位事务；输入准备前已排重',[b],minimal)
        rows = []
        for i,tid in enumerate(selection,1):
            rows.append(input_row(tid,assets))
            if i%500 == 0:
                print(json.dumps(dict(prepared=i,frozen_sample_size=SIZE)),flush=True)
        for name in ('generate','judge','revise','execute','id_rules'):
            freeze(RUN/'prompts'/(name+'.txt'),(HISTORY[0]/'prompts'/(name+'.txt')).read_text(),raw=True)
        freeze(RUN/'prompts'/STARTUP.name,STARTUP.read_text(),raw=True)
        for name,value in [('population',population),('excluded',excluded),('eligible',eligible),('selection',selection)]:
            freeze(RUN/(name+'.json'),value)
        if not (RUN/'historical_audit.json').exists():
            freeze(RUN/'historical_audit.json',audit)
        files = [Path(__file__),ROOT/'scripts/rubric_luna_dispatch.py',ROOT/'scripts/rubric_luna100.py',
                 ROOT/'scripts/rubric_luna_batch02.py',ROOT/'scripts/rubric_candidates_100.py',
                 ROOT/'scripts/rubric_candidates.py',ROOT/'scripts/rubric_pilot.py',
                 ROOT/'tests/test_rubric_luna_batch03.py',ROOT/'tests/test_rubric_luna_dispatch.py']
        m = dict(version='batch03-bounded-trial-v1',run_id=RUN_ID,dataset_id=legacy.DATASET_ID,
                 created_at=intent['created_at'],sample_size=SIZE,tasks=rows,
                 population_size=len(population),eligible_size=len(eligible),
                 sampling=dict(seed=SEED,method='random.Random(seed).sample(sorted(eligible_task_ids),sample_size)'),
                 source_file_hashes=hashes,prompt_hashes={p.name:digest(p) for p in (RUN/'prompts').glob('*.txt')},
                 implementation_hashes={str(p):digest(p) for p in files},
                 population_snapshots={n:dict(path=str(RUN/(n+'.json')),sha256=digest(RUN/(n+'.json'))) for n in ('population','excluded','eligible','selection')},
                 model=MODEL,reasoning_effort=EFFORT,workers=12,initial_concurrency=3,
                 target_concurrency=12,max_concurrency=12,concurrency=12,
                 automatic_retries=0,max_external_revisions=1,fork_turns='none',
                 semantic_review_by_coordinator=False,pass_rule='five valid integer scores in 0/1/2; none zero',
                 historical_ids=len(audit['ids']),historical_overlap=0,
                 source_method=read(HISTORY[0]/'manifest.json')['source_method'],
                 excluded_data=['database rows','private answers','historical rubrics','execution trajectories'],
                 trial_only=True,trial_max_calls=13,trial_dispatch_seconds=180,
                 promotion_enabled=False,promotion_note='A <=180s trial cannot meet the 300s observation minimum')
        freeze(RUN/'manifest.json',m)
        b = legacy.batch_record(m,b)
        b['status'] = 'reserved'
        legacy.commit('完整输入和本批manifest提交；占位事务完成',[b],
                      [legacy.task_record(m,t,RUN,RUN_ID) for t in rows])
        return m


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return dict(pid=pid,pgid=os.getpgid(pid),start_ticks=stat[19],
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (FileNotFoundError,ProcessLookupError):
        return dict(pid=pid,pgid=pid,already_exited=True)


def group_alive(pgid):
    try:
        os.killpg(pgid,0)
        return True
    except ProcessLookupError:
        return False


async def terminate_group(proc):
    for sig,seconds in [(signal.SIGTERM,5),(signal.SIGKILL,5)]:
        if group_alive(proc.pid):
            os.killpg(proc.pid,sig)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()),seconds)
        except asyncio.TimeoutError:
            continue
        deadline = time.monotonic()+seconds
        while group_alive(proc.pid) and time.monotonic()<deadline:
            await asyncio.sleep(.05)
        if not group_alive(proc.pid):
            return
    raise RuntimeError('process_group_not_confirmed_exited')


def orphan_check():
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
        except (OSError,ProcessLookupError):
            continue
        if len(args)>2 and b'codex' in Path(os.fsdecode(args[0])).name.encode() and b'exec' in args[1:3]:
            if any(str(RUN/'tasks').encode() in arg for arg in args):
                raise RuntimeError('prior_model_process_still_alive:'+path.parent.name)
    for folder in RUN.glob('tasks/*/round_*/*'):
        if (folder/'started.json').exists() and not (folder/'completed.json').exists():
            tid,_,done = legacy.parse_events(folder)
            if not done:
                raise RuntimeError('prior_request_unresolved_no_automatic_dispatch:'+str(folder))


class Trial:
    def __init__(self,m,seconds=180,max_calls=13,launcher=None):
        if not 0 < seconds <= 180 or not 1 <= max_calls <= 13:
            raise ValueError('trial_scope_exceeded')
        self.m,self.seconds,self.max_calls = m,seconds,max_calls
        self.launcher = launcher or asyncio.create_subprocess_exec
        self.tasks = {t['task_id']:t for t in m['tasks']}
        self.children,self.suspended,self.results = {},{},{}

    async def save_controller(self,event,state):
        def save():
            append(RUN/'dispatch_events.jsonl',dict(at=now(),event=event,state=state))
            snapshot(RUN/'scheduler_state.json',state)
        await self.writer.submit(save)

    def record(self,task_id,status=None):
        record = legacy.task_record(self.m,self.tasks[task_id],RUN,RUN_ID)
        if status:
            record['status'] = status
        rubrics = sorted((RUN/'tasks'/task_id.replace(':','__')).glob('round_*/rubric.json'))
        if rubrics and record['final_rubric_ref'] is None:
            record.update(final_rubric_ref=str(rubrics[-1]),final_version=int(rubrics[-1].parent.name.split('_')[1]))
        return record

    async def recover(self,folder,identity):
        if not (folder/'identity.json').exists():
            return None
        assert read(folder/'identity.json') == identity
        if (folder/'completed.json').exists():
            end = read(folder/'completed.json')
            assert end['identity'] == identity
            if end['status'] == 'completed':
                assert digest(folder/'response.txt') == end['response_sha256']
                assert read(folder/'usage.json')['actual_config_verified']
                return (folder/'response.txt').read_text()
            raise legacy.base.CallFailure(end['status'])
        if (folder/'started.json').exists():
            tid,_,done = legacy.parse_events(folder)
            if done and (folder/'response.txt').exists():
                usage = legacy.base.thread_usage(tid)
                if usage.get('actual_config_verified'):
                    await self.writer.submit(lambda: freeze(folder/'usage.json',{**identity,**usage}))
                    await self.writer.submit(lambda: freeze(folder/'completed.json',dict(identity=identity,status='completed',thread_id=tid,
                        response_sha256=digest(folder/'response.txt'),recovered_offline=True)))
                    return (folder/'response.txt').read_text()
            raise DispatchPaused('previous_request_unresolved')
        return None

    async def call(self,role,revision,task_id,payload,unused_sem):
        stage = legacy.stage_for(role,revision)
        folder = RUN/'tasks'/task_id.replace(':','__')/f'round_{revision}'/role
        pack = dict(role=role,execution=(RUN/'prompts/execute.txt').read_text(),
                    protocol=(RUN/'prompts'/(role+'.txt')).read_text(),data=payload)
        content = json.dumps(pack,ensure_ascii=False,separators=(',',':'))
        identity = dict(run_id=RUN_ID,task_id=task_id,stage=stage,revision=revision,
                        model=MODEL,reasoning_effort=EFFORT,input_sha256=hashlib.sha256(content.encode()).hexdigest(),
                        prompt_sha256=digest(RUN/'prompts'/(role+'.txt')),
                        execution_sha256=digest(RUN/'prompts/execute.txt'),fork_turns='none')
        cached = await self.recover(folder,identity)
        if cached is not None:
            return cached
        key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        message = '按以下完整输入包的execution和protocol处理单题单阶段。使用消息模式，只返回业务JSON，不调用工具，不读取文件，不启动子agent。\n'+content
        cmd = ['codex','exec','--skip-git-repo-check','--ignore-user-config','-m',MODEL,
               '-c','model_reasoning_effort="high"','-s','read-only','--json','-o',str(folder/'response.pending.txt'),'-']
        proc,out,err,communication = None,None,None,None
        entered,done,usage,unavailable,rate_limited = False,False,None,False,False
        started = time.monotonic()
        async def before():
            def persist():
                freeze(folder/'input.json',pack)
                freeze(folder/'identity.json',identity)
                registry_event('阶段请求身份持久化；待派发',tasks=[self.record(task_id,'running')])
            await self.writer.submit(persist)
        async def spawn():
            nonlocal out,err
            out = (folder/'events.jsonl').open('xb')
            err = (folder/'stderr.txt').open('xb')
            return await self.launcher(*cmd,stdin=asyncio.subprocess.PIPE,stdout=out,stderr=err,
                                       cwd=str(ROOT),start_new_session=True)
        try:
            proc,cancelled = await self.gate.launch(key,before,spawn)
            entered = True
            self.children[key] = proc
            pid = process_identity(proc.pid)
            await self.writer.submit(lambda: freeze(folder/'started.json',dict(identity=identity,started_at=now(),process=pid)))
            await self.gate.spawned(key,pid)
            communication = asyncio.create_task(proc.communicate(message.encode()))
            if cancelled:
                raise asyncio.CancelledError()
            await asyncio.shield(communication)
            tid,unavailable,done = legacy.parse_events(folder)
            if (folder/'response.pending.txt').exists():
                response = (folder/'response.pending.txt').read_text()
                await self.writer.submit(lambda: freeze(folder/'response.txt',response,raw=True))
            usage = await asyncio.to_thread(legacy.base.thread_usage,tid) if tid else dict(usage_unknown=True,error='no_thread')
            await self.writer.submit(lambda: freeze(folder/'usage.json',{**identity,**usage}))
            model_bad = unavailable or (usage.get('model') is not None and not usage.get('actual_config_verified'))
            success = proc.returncode==0 and done and (folder/'response.txt').exists() and usage.get('actual_config_verified')
            status = 'completed' if success else ('model_unavailable' if model_bad else 'call_failed')
            await self.writer.submit(lambda: freeze(folder/'completed.json',dict(identity=identity,status=status,
                returncode=proc.returncode,thread_id=tid,finished_at=now(),server_completion_evidence=done,
                response_sha256=digest(folder/'response.txt') if (folder/'response.txt').exists() else None)))
            raw_events = (folder/'events.jsonl').read_text()+'\n'+(folder/'stderr.txt').read_text()
            rate_limited = bool(re.search(r'\b429\b|rate.limit|too many requests',raw_events,re.I))
            retry_match = re.search(r'(?:retry.after|try again in)[^\d]{0,8}(\d+(?:\.\d+)?)',raw_events,re.I)
            retry_after = float(retry_match.group(1)) if retry_match else None
            # A clean local exit with no server completion still retains a reservation.
            if group_alive(proc.pid):
                await finish_cleanup(terminate_group(proc))
            await self.gate.complete(key,infrastructure_failure=not success,model_bad=model_bad,
                unresolved=not done,rate_limited=rate_limited,retry_after=retry_after,
                detail=dict(stage=stage,elapsed_seconds=time.monotonic()-started,
                            input_bytes=len(message.encode()),success=bool(success)))
            entered = False
            await self.writer.submit(lambda: registry_event('阶段原始响应及用量已提交',tasks=[self.record(task_id)]))
            print(json.dumps(dict(task_id=task_id,stage=stage,status=status,
                                 tokens=(usage.get('actual_usage') or {}).get('total_tokens'))),flush=True)
            if model_bad:
                raise legacy.base.ModelUnavailable('required_model_configuration')
            if not success:
                raise legacy.base.CallFailure(status)
            return (folder/'response.txt').read_text()
        except (DispatchPaused,legacy.base.CallFailure,legacy.base.ModelUnavailable):
            raise
        except BaseException:
            self.gate.requested_stop = 'call_lifecycle_error'
            raise
        finally:
            async def clean():
                nonlocal entered
                if proc is not None:
                    if proc.returncode is None or group_alive(proc.pid):
                        await terminate_group(proc)
                    if communication is not None:
                        await communication
                    self.children.pop(key,None)
                if entered:
                    _,_,completed = legacy.parse_events(folder)
                    await self.gate.complete(key,infrastructure_failure=True,unresolved=not completed,
                                             detail=dict(stage=stage,cleanup=True))
                if out is not None:
                    out.close()
                if err is not None:
                    err.close()
            await finish_cleanup(clean())

    async def run(self):
        configure()
        orphan_check()
        self.writer = Writer()
        prior = read(RUN/'scheduler_state.json') if (RUN/'scheduler_state.json').exists() else None
        self.gate = Gate(self.save_controller,limit=3,max_calls=self.max_calls,state=prior)
        legacy.base.call = self.call
        started = time.monotonic()
        await self.writer.submit(lambda: snapshot(RUN/'trial_authorization.json',dict(
            at=now(),max_calls=self.max_calls,dispatch_seconds=self.seconds,
            initial_concurrency=3,worker_capacity=12,purpose='user-requested bounded real-pipeline trial',
            startup_prompt_sha256=digest(STARTUP))))
        queue = asyncio.Queue()
        for t in self.m['tasks']:
            if not (RUN/'tasks'/t['task_id'].replace(':','__')/'status.json').exists():
                queue.put_nowait(t)
        async def timer():
            try:
                await asyncio.wait_for(self.gate.stopped.wait(),self.seconds)
            except asyncio.TimeoutError:
                await self.gate.stop('trial_dispatch_deadline')
        async def worker():
            while not queue.empty() and not self.gate.requested_stop and not self.gate.stop_reason:
                task = queue.get_nowait()
                tid = task['task_id']
                try:
                    await self.writer.submit(lambda: snapshot(RUN/'task_claims'/tid.replace(':','__'),
                        dict(task_id=tid,owner_pid=os.getpid(),at=now())))
                    state = await legacy.RAW_PROCESS(task,None)
                    if any(read(p).get('usage_unknown') for p in (RUN/'tasks'/tid.replace(':','__')).glob('round_*/*/usage.json')):
                        state['status'] = 'usage_unknown'
                        await self.writer.submit(lambda: snapshot(RUN/'tasks'/tid.replace(':','__')/'status.json',state))
                    self.results[tid] = state
                    await self.writer.submit(lambda: registry_event('逐题终态',tasks=[self.record(tid)]))
                except DispatchPaused as exc:
                    self.suspended[tid] = str(exc)
                    await self.writer.submit(lambda: registry_event('限时停止；保留阶段断点',tasks=[self.record(tid,'interrupted')]))
                except legacy.base.ModelUnavailable:
                    await self.gate.stop('model_configuration')
                except Exception as exc:
                    self.suspended[tid] = repr(exc)
                    await self.gate.stop('infrastructure_or_persistence_error')
                finally:
                    queue.task_done()
        loop = asyncio.get_running_loop()
        def user_stop():
            self.gate.requested_stop = 'user_stop'
            asyncio.create_task(self.gate.stop('user_stop'))
        for sig in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(sig,user_stop)
        workers = [asyncio.create_task(worker()) for _ in range(12)]
        timer_task = asyncio.create_task(timer())
        try:
            await self.gate.stopped.wait()
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*workers)),60)
            except asyncio.TimeoutError:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers,return_exceptions=True)
        finally:
            timer_task.cancel()
            await asyncio.gather(timer_task,return_exceptions=True)
            for sig in (signal.SIGTERM,signal.SIGINT):
                loop.remove_signal_handler(sig)
            await finish_cleanup(self.writer.close())
        summary = dict(started_at=read(RUN/'trial_authorization.json')['at'],finished_at=now(),
            wall_seconds=time.monotonic()-started,dispatch_limit_seconds=self.seconds,
            calls_launched=self.gate.launched,peak_concurrency=self.gate.peak,
            current_concurrency_limit=self.gate.limit,stop_reason=self.gate.stop_reason,
            unresolved_remote_requests=len(self.gate.remote_holds),remaining_local_children=len(self.children),
            suspended=self.suspended,terminal_this_trial=len(self.results),
            initial_concurrency=3,promotion_eligible=False)
        snapshot(RUN/'trial_summary.json',summary)
        return summary


def report(m):
    configure()
    usages = [read(p) for p in RUN.glob('tasks/*/round_*/*/usage.json')]
    usage = legacy.usage_summary(usages)
    snapshot(RUN/'usage.json',usage)
    states = {s['task_id']:s for s in (read(p) for p in RUN.glob('tasks/*/status.json'))}
    passed = sum(s['status']=='passed' for s in states.values())
    result = dict(completed=len(states)==m['sample_size'],denominator=m['sample_size'],
        terminal_count=len(states),tasks=list(states.values()),final_passed=passed,
        first_passed=sum(s.get('first_status')=='passed' for s in states.values()),
        revision_tasks=sum(s.get('entered_revision',False) for s in states.values()),
        revision_additional_passed=sum(s['status']=='passed' and s.get('entered_revision',False) for s in states.values()),
        failure_types=dict(collections.Counter(s['status'] for s in states.values() if s['status']!='passed')),
        totals=usage['totals'],stages=usage['stages'],unknown_usage_calls=len(usage['unknown_usage']),
        tokens_per_original=None,tokens_per_accepted=usage['totals']['total_tokens']/passed if passed else None,
        semantic_review_by_coordinator=False)
    snapshot(RUN/'results.json',result)
    snapshot(RUN/'failures.json',[s for s in states.values() if s['status']!='passed'])
    accepted = [dict(task_id=t,path=str(p),sha256=digest(p)) for t,s in states.items() if s['status']=='passed'
                for p in [RUN/'accepted'/(t.replace(':','__')+'.json')]]
    snapshot(RUN/'accepted_manifest.json',accepted)
    snapshot(RUN/'retention_manifest.json',dict(frozen_manifest_sha256=digest(RUN/'manifest.json'),
             tasks=[dict(task_id=t['task_id'],original_retained=True,status=states.get(t['task_id'],{}).get('status','reserved')) for t in m['tasks']]))
    with legacy.locked(REGISTRY/'.lock'):
        bs,_ = legacy.replay()
        b = legacy.batch_record(m,bs[RUN_ID],result,usage)
        b['status'] = 'partial_user_stopped'
        records = []
        for t in m['tasks']:
            rec = legacy.task_record(m,t,RUN,RUN_ID)
            if rec['status']=='running':
                rec['status']='interrupted'
            records.append(rec)
        legacy.commit('试跑结束；刷新登记投影及去重用量',[b],records)
        legacy.cumulative_report()
    summary = read(RUN/'trial_summary.json') if (RUN/'trial_summary.json').exists() else {}
    statuses = dict(collections.Counter(t['status'] for t in records))
    snapshot(RUN/'progress.json',dict(frozen=SIZE,terminal=len(states),passed=passed,statuses=statuses,at=now()))
    text = [f'# 新版3000题启动提示词：正式流水线有限试跑', '',
        f'本批冻结{SIZE}题，历史4000题交集为0，统一登记覆盖7000题。试跑仅为部分完成：{len(states)}/{SIZE}题终态，通过{passed}题。', '',
        '业务prompt、单题来源和最多一次修订状态机沿用历史实现；独立新入口只负责准备、派发、进程生命周期及记账，不做语义评分。', '',
        '## 试跑耗时与并发', '', '```json',json.dumps(summary,ensure_ascii=False,indent=2),'```','',
        '## 阶段用量', '', '| 阶段 | 调用数 | 已知tokens | 未知用量 |','|---|---:|---:|---:|']
    for stage,u in usage['stages'].items():
        text.append(f"| {stage} | {u['threads']} | {u['total_tokens']:,} | {u['unknown_usage_calls']} |")
    text += ['',f"已知Luna用量合计 **{usage['totals']['total_tokens']:,} tokens**；未知{len(usage['unknown_usage'])}项。主调度聊天不计入此数；思考/缓存是子项。",'',
        '没有另跑付费串行对照；历史同阶段成本及本轮各调用的输入/输出/缓存记录见usage.json。尚无完整的3000题每题成本，不能用已知部分除3000冒充完整成本。', '',
        '## 验证边界', '',
        '本入口仅开放<=180秒、<=13次调用的初始3并发试跑；没有开放全量run命令。3分钟无法达到每档至少5分钟的升档要求，因此未升档。自动升档、可比窗口P95退档和最终统一题库发布仍须在全量执行前另行实现/验收；不把本轮称为完整生产调度验收通过。', '',
        '停止后保留原manifest、3000题占位和全部已调用阶段。已完成阶段仅在身份哈希相同且实际配置正确时复用；未知请求不自动重发。原始请求、事件、response、usage保存在tasks目录，动态门控及熔断/冷却快照见scheduler_state.json。', '',
        '后续有限试跑命令：`agent-world-model/.venv/bin/python scripts/rubric_luna_batch03.py trial --seconds 180 --max-calls 13`；仅在用户再次要求续跑、已核对无存活旧进程且故障消除时执行。该命令不解除基础设施熔断或未知请求占用。']
    atomic(RUN/'report_zh.md','\n'.join(text)+'\n')
    # Preserve existing registry guidance and append the new continuation entry.
    addition = '\n\n## 新增3000题（batch03）\n\n本批已冻结占位；有限试跑报告：runs/rubric_luna_high_3000_batch03/report_zh.md。入口scripts/rubric_luna_batch03.py只开放prepare、trial、report。trial默认3并发、180秒停止派发、最多13次调用；不开放全量生产命令。按原manifest续跑，不重新抽样。用户停止后不自动恢复；未知请求/存活旧进程必须先核对。\n'
    if '## 新增3000题（batch03）' not in (REGISTRY/'README.md').read_text():
        atomic(REGISTRY/'README.md',(REGISTRY/'README.md').read_text()+addition)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','trial','report'])
    parser.add_argument('--seconds',type=float,default=180)
    parser.add_argument('--max-calls',type=int,default=13)
    args = parser.parse_args()
    if not 0 < args.seconds <= 180 or not 1 <= args.max_calls <= 13:
        parser.error('bounded trial requires 0<seconds<=180 and 1<=max-calls<=13')
    configure()
    with legacy.locked(RUN/'.runner.lock',blocking=False):
        m = prepare() if args.action!='report' else read(RUN/'manifest.json')
        if args.action=='trial':
            legacy.verify(m)
            asyncio.run(Trial(m,args.seconds,args.max_calls).run())
            report(m)
        elif args.action=='report':
            report(m)


if __name__=='__main__':
    main()
