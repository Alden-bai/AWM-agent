"""Resume only frozen batch03 through completion and atomic unified publication.

Run with the same batch lock as prior entries. No resampling or paid preflight.
"""
import argparse
import asyncio
import collections
import copy
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import time

import rubric_luna_batch03 as batch
from rubric_luna_runtime_v1 import DispatchPaused, Writer, finish_cleanup
from rubric_luna_autoscale_v1 import AdaptiveGate

legacy = batch.legacy
ROOT,RUN,RUN_ID,REGISTRY = batch.ROOT,batch.RUN,batch.RUN_ID,batch.REGISTRY
MODEL,EFFORT = batch.MODEL,batch.EFFORT
read,digest,now = batch.read,batch.digest,batch.now
freeze,snapshot,append = batch.freeze,batch.snapshot,batch.append
process_identity,group_alive,terminate_group = batch.process_identity,batch.group_alive,batch.terminate_group


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def measured_usage(thread_id,spawn_wall):
    usage = legacy.base.thread_usage(thread_id)
    starts,ends = [],[]
    if usage.get('rollout_ref'):
        for line in Path(usage['rollout_ref']).read_text().splitlines():
            item = json.loads(line)
            p = item.get('payload',{})
            if item['type']=='event_msg' and p.get('type')=='task_started':
                starts.append(timestamp(item['timestamp']))
            if item['type']=='event_msg' and p.get('type')=='task_complete':
                ends.append(timestamp(item['timestamp']))
    # Missing raw timing remains unknown, never replaced with queue+process time.
    usage['timing'] = dict(startup_seconds=max(0,starts[0]-spawn_wall) if starts else None,
                          model_seconds=ends[-1]-starts[0] if len(starts)==len(ends)==1 else None,
                          definition='spawn to raw task_started; raw task_started to task_complete')
    return usage


def transport_errors(folder):
    rows = [(folder/'stderr.txt').read_text()]
    for line in (folder/'events.jsonl').read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') in ('error','turn.failed'):
            rows.append(json.dumps(event,ensure_ascii=False))
    return '\n'.join(rows)


class Production(batch.Trial):
    def __init__(self,m,launcher=None):
        super().__init__(m,launcher=launcher)
        self.claimed = set()
        self.started_wall = time.time()
        self.baseline_terminal = 0
        self.completed_rows = {}
        self.usage_rows = {}

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
        requested_at = time.monotonic()
        slot_at = spawn_at = spawned_at = finished_at = None
        spawn_wall = None
        async def before():
            nonlocal slot_at
            slot_at = time.monotonic()
            def persist():
                freeze(folder/'input.json',pack)
                freeze(folder/'identity.json',identity)
                batch.registry_event('阶段请求身份持久化；待派发',tasks=[self.record(task_id,'running')])
            await self.writer.submit(persist)
        async def spawn():
            nonlocal out,err,spawn_at,spawned_at,spawn_wall
            out = (folder/'events.jsonl').open('xb')
            err = (folder/'stderr.txt').open('xb')
            spawn_at = time.monotonic()
            spawn_wall = time.time()
            result = await self.launcher(*cmd,stdin=asyncio.subprocess.PIPE,stdout=out,stderr=err,
                                       cwd=str(ROOT),start_new_session=True)
            spawned_at = time.monotonic()
            return result
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
            finished_at = time.monotonic()
            tid,unavailable,done = legacy.parse_events(folder)
            if (folder/'response.pending.txt').exists():
                response = (folder/'response.pending.txt').read_text()
                await self.writer.submit(lambda: freeze(folder/'response.txt',response,raw=True))
            usage = await asyncio.to_thread(measured_usage,tid,spawn_wall) if tid else dict(usage_unknown=True,error='no_thread')
            await self.writer.submit(lambda: freeze(folder/'usage.json',{**identity,**usage}))
            model_bad = unavailable or (usage.get('model') is not None and not usage.get('actual_config_verified'))
            success = proc.returncode==0 and done and (folder/'response.txt').exists() and usage.get('actual_config_verified') and not usage.get('tool_call_count') and not usage.get('usage_unknown')
            status = 'completed' if success else ('model_unavailable' if model_bad else 'call_failed')
            await self.writer.submit(lambda: freeze(folder/'completed.json',dict(identity=identity,status=status,
                returncode=proc.returncode,thread_id=tid,finished_at=now(),server_completion_evidence=done,
                response_sha256=digest(folder/'response.txt') if (folder/'response.txt').exists() else None)))
            raw_events = transport_errors(folder)
            rate_limited = bool(re.search(r'\b429\b|rate.limit|too many requests',raw_events,re.I))
            retry_match = re.search(r'(?:retry.after|try again in)[^\d]{0,8}(\d+(?:\.\d+)?)',raw_events,re.I)
            retry_after = float(retry_match.group(1)) if retry_match else None
            # A clean local exit with no server completion still retains a reservation.
            if group_alive(proc.pid):
                await finish_cleanup(terminate_group(proc))
            await self.gate.complete(key,infrastructure_failure=not success,model_bad=model_bad,
                unresolved=not done,rate_limited=rate_limited,retry_after=retry_after,
                detail=dict(stage=stage,queue_seconds=slot_at-requested_at,
                    intent_seconds=spawn_at-slot_at,spawn_seconds=spawned_at-spawn_at,
                    process_seconds=finished_at-spawned_at,
                    startup_seconds=(usage.get('timing') or {}).get('startup_seconds'),
                    model_seconds=(usage.get('timing') or {}).get('model_seconds'),
                    input_bytes=len(message.encode()),success=bool(success),
                    startup_warning_count=len(re.findall('model.*(?:refresh|list).*timed? ?out|failed to refresh.*model',raw_events,re.I))))
            entered = False
            await self.writer.submit(lambda: batch.registry_event('阶段原始响应及用量已提交',tasks=[self.record(task_id)]))
            print(json.dumps(dict(task_id=task_id,stage=stage,status=status,
                                 tokens=(usage.get('actual_usage') or {}).get('total_tokens'))),flush=True)
            if model_bad:
                raise legacy.base.ModelUnavailable('required_model_configuration')
            if not done:
                raise DispatchPaused('remote_completion_unknown_no_retry')
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

    def compact_progress(self):
        elapsed = time.time()-self.started_wall
        terminal = len(self.completed_rows)
        new = terminal-self.baseline_terminal
        rate = new/max(elapsed,1)*3600
        states = collections.Counter(s['status'] for s in self.completed_rows.values())
        states['pending'] = self.m['sample_size']-terminal
        usage = legacy.usage_summary(list(self.usage_rows.values()))
        recent = [r for r in self.gate.history if r['completed_at']>=max(self.started_wall,time.time()-600)]
        duration = max(1,min(600,time.time()-self.started_wall))
        performance = {}
        for stage in legacy.base.STAGES:
            rows = [r for r in recent if r.get('stage')==stage]
            values = {}
            for metric in ('queue_seconds','intent_seconds','spawn_seconds','startup_seconds','process_seconds','model_seconds'):
                samples = sorted(r[metric] for r in rows if r.get(metric) is not None)
                values[metric] = dict(n=len(samples),p95=samples[math.ceil(.95*len(samples))-1],
                    mean=sum(samples)/len(samples)) if samples else None
            performance[stage] = dict(completed=len(rows),successful_per_minute=sum(bool(r.get('success')) for r in rows)/duration*60,
                latency=values,input_bytes_range=[min(r['input_bytes'] for r in rows if 'input_bytes' in r),max(r['input_bytes'] for r in rows if 'input_bytes' in r)] if any('input_bytes' in r for r in rows) else None)
        snapshot(RUN/'progress.json',dict(at=now(),frozen=self.m['sample_size'],terminal=terminal,
            passed=states['passed'],statuses=dict(states),elapsed_seconds=elapsed,
            terminal_per_hour=rate,estimated_remaining_hours=(self.m['sample_size']-terminal)/rate if rate else None,
            concurrency_limit=self.gate.limit,active=len(self.gate.active),peak=self.gate.peak,
            stop_reason=self.gate.stop_reason or self.gate.requested_stop,
            luna_known_tokens=usage['totals']['total_tokens'],unknown_usage_calls=len(usage['unknown_usage']),
            last_autoscale_decision=self.gate.last_decision,rolling_stage_performance=performance,
            observation_minutes=elapsed/60,observation_counts_in_batch=True))
        if elapsed>=1800 and not (RUN/'production_observation.json').exists():
            snapshot(RUN/'production_observation.json',dict(at=now(),elapsed_seconds=elapsed,
                terminal=terminal,new_terminal=new,limit=self.gate.limit,peak=self.gate.peak,
                stop_reason=self.gate.stop_reason,remote_holds=self.gate.remote_holds,
                windows=self.gate.windows,performance=performance,
                automatic_continuation=not bool(self.gate.stop_reason or self.gate.requested_stop),
                insufficient_promotion_evidence_keeps_current_limit=True))
        snapshot(RUN/'queue_state.json',dict(at=now(),manifest_sha256=digest(RUN/'manifest.json'),
            terminal_ids=sorted(self.completed_rows),claimed_ids=sorted(self.claimed-set(self.completed_rows)),
            pending_ids=[t['task_id'] for t in self.m['tasks'] if t['task_id'] not in self.claimed and t['task_id'] not in self.completed_rows]))

    async def save_controller(self,event,state):
        def save():
            event_state = copy.deepcopy(state)
            event_state.pop('stage_completions',None)
            scale = event_state.get('autoscale',{})
            scale.pop('rows',None)
            if event!='autoscale_window':
                scale.pop('windows',None)
                scale.pop('stable',None)
            append(RUN/'dispatch_events.jsonl',dict(at=now(),event=event,state=event_state,
                completion=state['stage_completions'][-1] if event=='stage_completed' else None))
            snapshot(RUN/'scheduler_state.json',state)
        await self.writer.submit(save)

    async def run(self):
        batch.configure()
        self.writer = Writer(maxsize=32)
        prior = read(RUN/'scheduler_state.json') if (RUN/'scheduler_state.json').exists() else None
        self.gate = AdaptiveGate(self.save_controller,state=prior)
        legacy.base.call = self.call
        self.completed_rows = {s['task_id']:s for s in (read(p) for p in RUN.glob('tasks/*/status.json'))}
        self.usage_rows = {str(p):read(p) for p in RUN.glob('tasks/*/round_*/*/usage.json')}
        self.baseline_terminal = len(self.completed_rows)
        queue = asyncio.Queue()
        for task in self.m['tasks']:
            if task['task_id'] not in self.completed_rows:
                queue.put_nowait(task)
        await self.writer.submit(lambda: append(RUN/'production_runs.jsonl',dict(at=now(),pid=os.getpid(),
            frozen=self.m['sample_size'],existing_terminal=self.baseline_terminal,pending=queue.qsize(),
            user_authorized_full_continuation=True,observation_seconds=1800,max_observation_seconds=3600)))
        async def worker():
            while not queue.empty() and not self.gate.requested_stop and not self.gate.stop_reason:
                task = queue.get_nowait()
                tid = task['task_id']
                try:
                    if tid in self.claimed:
                        raise RuntimeError('duplicate_task_claim:'+tid)
                    self.claimed.add(tid)
                    await self.writer.submit(lambda: snapshot(RUN/'task_claims'/tid.replace(':','__'),
                        dict(task_id=tid,owner_pid=os.getpid(),process=process_identity(os.getpid()),at=now())))
                    state = await legacy.RAW_PROCESS(task,None)
                    fresh = {str(p):read(p) for p in (RUN/'tasks'/tid.replace(':','__')).glob('round_*/*/usage.json')}
                    if any(u.get('usage_unknown') for u in fresh.values()):
                        state['status'] = 'usage_unknown'
                        await self.writer.submit(lambda: snapshot(RUN/'tasks'/tid.replace(':','__')/'status.json',state))
                    self.usage_rows.update(fresh)
                    self.completed_rows[tid] = state
                    self.results[tid] = state
                    await self.writer.submit(lambda: batch.registry_event('正式续跑逐题终态',tasks=[self.record(tid)]))
                    if len(self.results)%50==0:
                        await self.writer.submit(self.compact_progress)
                except DispatchPaused as exc:
                    self.suspended[tid] = str(exc)
                    await self.writer.submit(lambda: batch.registry_event('停止派发；保留原阶段断点',tasks=[self.record(tid,'interrupted')]))
                except legacy.base.ModelUnavailable:
                    await self.gate.stop('model_configuration')
                except Exception as exc:
                    self.suspended[tid] = repr(exc)
                    await self.gate.stop('infrastructure_or_persistence_error')
                finally:
                    queue.task_done()
        async def monitor():
            try:
                while not self.gate.stopped.is_set():
                    await asyncio.sleep(15)
                    await self.gate.observe()
                    await self.writer.submit(self.compact_progress)
                    print(json.dumps(dict(progress=read(RUN/'progress.json'))),flush=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.gate.requested_stop = 'monitor_or_writer_failure'
                await self.gate.stop('monitor_or_writer_failure')
                raise
        loop = asyncio.get_running_loop()
        def user_stop():
            self.gate.requested_stop = 'user_stop'
            asyncio.create_task(self.gate.stop('user_stop'))
        for sig in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(sig,user_stop)
        workers = [asyncio.create_task(worker()) for _ in range(12)]
        watcher = asyncio.create_task(monitor())
        try:
            # Healthy requests drain without inheriting the trial's 60-second timeout.
            await asyncio.gather(*workers)
            if not self.gate.stop_reason:
                await self.gate.stop('batch_complete' if len(self.completed_rows)==self.m['sample_size'] else 'queue_interrupted')
        finally:
            self.gate.requested_stop = self.gate.requested_stop or self.gate.stop_reason or 'runner_exiting'
            # Never release the run lock while any worker still owns a process.
            await asyncio.gather(*workers,return_exceptions=True)
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
            for sig in (signal.SIGTERM,signal.SIGINT):
                loop.remove_signal_handler(sig)
            # Include completed in-flight stages of interrupted tasks in the final accounting.
            self.usage_rows = {str(p):read(p) for p in RUN.glob('tasks/*/round_*/*/usage.json')}
            try:
                await self.writer.submit(self.compact_progress)
            finally:
                await finish_cleanup(self.writer.close())
        summary = dict(started_at=datetime.datetime.fromtimestamp(self.started_wall,datetime.timezone.utc).isoformat(),
            finished_at=now(),wall_seconds=time.time()-self.started_wall,
            terminal_count=len(self.completed_rows),new_terminal=len(self.results),
            calls_launched=self.gate.launched,peak=self.gate.peak,limit=self.gate.limit,
            stop_reason=self.gate.stop_reason,remote_holds=self.gate.remote_holds,suspended=self.suspended)
        snapshot(RUN/'production_summary.json',summary)
        return summary


def reconcile_requests():
    """Read raw completion evidence before clearing any persisted active reservation."""
    batch.orphan_check()
    state = read(RUN/'scheduler_state.json')
    if state.get('stop_reason') not in (None,'pilot_scope_complete','trial_call_limit','trial_dispatch_deadline',
                                       'user_stop','batch_complete','queue_interrupted'):
        raise RuntimeError('prior_failure_requires_evidence_and_explicit_reconciliation:'+str(state.get('stop_reason')))
    unfinished = []
    for identity_path in RUN.glob('tasks/*/round_*/*/identity.json'):
        folder = identity_path.parent
        if (folder/'started.json').exists():
            identity = read(folder/'started.json')['process']
            if group_alive(identity['pgid']):
                raise RuntimeError('prior_process_group_still_alive:'+str(identity))
        if (folder/'completed.json').exists():
            end = read(folder/'completed.json')
            if end.get('status')=='completed':
                assert end['identity']==read(identity_path)
                assert digest(folder/'response.txt')==end['response_sha256']
                assert read(folder/'usage.json')['actual_config_verified']
            elif not end.get('server_completion_evidence'):
                unfinished.append(str(folder))
        elif (folder/'events.jsonl').exists() or (folder/'started.json').exists():
            tid,_,done = legacy.parse_events(folder)
            if not done:
                unfinished.append(str(folder))
                continue
            response = folder/'response.txt'
            if not response.exists() and (folder/'response.pending.txt').exists():
                freeze(response,(folder/'response.pending.txt').read_text(),raw=True)
            usage = legacy.base.thread_usage(tid)
            if not response.exists() or not usage.get('actual_config_verified'):
                unfinished.append(str(folder))
                continue
            identity = read(identity_path)
            freeze(folder/'usage.json',{**identity,**usage})
            freeze(folder/'completed.json',dict(identity=identity,status='completed',thread_id=tid,
                response_sha256=digest(response),recovered_offline=True,server_completion_evidence=True))
    if unfinished:
        raise RuntimeError('unresolved_requests_no_automatic_retry:'+repr(unfinished))
    # Every persisted active/remote identity needs positive completion evidence.
    completed_keys = set()
    for path in RUN.glob('tasks/*/round_*/*/completed.json'):
        end = read(path)
        if end.get('server_completion_evidence'):
            completed_keys.add(hashlib.sha256(json.dumps(end['identity'],sort_keys=True).encode()).hexdigest())
    held = set(state.get('active',{}))|set(state.get('remote_holds',{}))
    if not held <= completed_keys:
        raise RuntimeError('persisted_remote_holds_not_reconciled')
    if held:
        append(RUN/'reconciliation_events.jsonl',dict(at=now(),released_with_completion_evidence=sorted(held)))
        state['active'],state['remote_holds'] = {},{}
        snapshot(RUN/'scheduler_state.json',state)
    return dict(at=now(),old_processes_exited=True,unresolved_requests=0,
                terminal_files=len(list(RUN.glob('tasks/*/status.json'))),run_lock_held=True)


def freeze_execution(acceptance):
    m,chain = legacy.execution_manifest(RUN)
    if m.get('production_entry')==str(Path(__file__).resolve()):
        return m
    execution = copy.deepcopy(m)
    execution.update(original_frozen_manifest_ref=str(RUN/'manifest.json'),
        original_frozen_manifest_sha256=digest(RUN/'manifest.json'),previous_execution_manifest_ref=chain[-1]['path'],
        created_for_execution_at=now(),production_entry=str(Path(__file__).resolve()),
        trial_only=False,promotion_enabled=True,production_acceptance_ref=str(acceptance),
        production_acceptance_sha256=digest(acceptance),
        policy=dict(levels=[3,6,9,12],min_level_seconds=300,min_completed_stages=30,
            input_size_ratio=1.5,min_matched_coverage=.8,min_stratum_calls=5,
            throughput_improvement_ratio=1.05,p95_degradation_ratio=2,bad_windows_to_downshift=2,
            initial_observation_seconds=1800,observation_results_in_batch=True,
            new_task_limit=0,max_external_revisions=1))
    for old_field in ('bounded_pilot','trial_max_calls','trial_dispatch_seconds','promotion_note'):
        execution.pop(old_field,None)
    execution['version'] = 'batch03-production-v1'
    execution.pop('diagnostic_override',None)
    for name in ('rubric_luna_batch03_production_v1.py','rubric_luna_runtime_v1.py',
                 'rubric_luna_autoscale_v1.py','rubric_luna_bank_v1.py'):
        path = ROOT/'scripts'/name
        execution['implementation_hashes'][str(path)] = digest(path)
    test = ROOT/'tests/test_rubric_luna_production_v1.py'
    execution['implementation_hashes'][str(test)] = digest(test)
    freeze(RUN/f'execution_manifest_v{len(chain)}.json',execution)
    with legacy.locked(REGISTRY/'.lock'):
        batches,_ = legacy.replay()
        record = legacy.batch_record(execution,batches[RUN_ID])
        record.update(status='running',production_entry=execution['production_entry'],
                      production_started_at=now(),production_policy=execution['policy'])
        legacy.commit('用户授权原3000题正式续跑；离线验收通过；原13题终态保留',[record])
    return execution


def report(m):
    """Use the frozen deterministic aggregation, replacing trial-specific text."""
    states = {s['task_id']:s for s in (read(p) for p in RUN.glob('tasks/*/status.json'))}
    assert set(states)<={t['task_id'] for t in m['tasks']}
    usage = legacy.usage_summary([read(p) for p in RUN.glob('tasks/*/round_*/*/usage.json')])
    snapshot(RUN/'usage.json',usage)
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
    snapshot(RUN/'failures.json',[s for s in states.values() if s['status']!='passed'])
    snapshot(RUN/'accepted_manifest.json',[dict(task_id=tid,path=str(path),sha256=digest(path))
        for tid,state in states.items() if state['status']=='passed'
        for path in [RUN/'accepted'/(tid.replace(':','__')+'.json')]])
    snapshot(RUN/'retention_manifest.json',dict(frozen_manifest_sha256=digest(RUN/'manifest.json'),
        tasks=[dict(task_id=t['task_id'],original_retained=True,status=states.get(t['task_id'],{}).get('status','reserved')) for t in m['tasks']]))
    if result['completed']:
        result['tokens_per_original'] = result['totals']['total_tokens']/m['sample_size']
    snapshot(RUN/'results.json',result)
    with legacy.locked(REGISTRY/'.lock'):
        batches,_ = legacy.replay()
        record = legacy.batch_record(m,batches[RUN_ID],result,read(RUN/'usage.json'))
        record['status'] = 'completed' if result['completed'] else 'partial_infrastructure_stopped'
        if result['completed']:
            record['finished_at'] = record.get('finished_at') or now()
        records = []
        for task in m['tasks']:
            row = legacy.task_record(m,task,RUN,RUN_ID)
            if row['status']=='running':
                row['status']='interrupted'
            records.append(row)
        legacy.commit('正式续跑最终汇总；保留历史未知用量',[record],records)
        legacy.cumulative_report()
    summary = read(RUN/'production_summary.json') if (RUN/'production_summary.json').exists() else {}
    lines = ['# 原3000题正式续跑',f'状态：{"全部终态" if result["completed"] else "部分完成"}；冻结{m["sample_size"]}题，终态{result["terminal_count"]}题，通过{result["final_passed"]}题。',
        f'首次通过{result["first_passed"]}；修订{result["revision_tasks"]}；修订新增通过{result["revision_additional_passed"]}。',
        '失败分类：'+json.dumps(result['failure_types'],ensure_ascii=False),
        '原13题终态复用；没有重抽样。业务prompt及完整来源不变，Luna/high独立会话、最多一次修订、五维无0通过；主调度没有语义评分。',
        '运行摘要：'+json.dumps(summary,ensure_ascii=False),
        '阶段用量：'+json.dumps(result['stages'],ensure_ascii=False),
        '总用量：'+json.dumps(result['totals'],ensure_ascii=False),
        f'未知用量{result["unknown_usage_calls"]}；每原题{result["tokens_per_original"]}；每通过版{result["tokens_per_accepted"]}（含失败成本）。',
        '主调度无法准确归属，另列scheduler_usage.json；历史3项未知保留，已知用量不代表完整账单。',
        '排队、持久化、进程启动、会话启动及模型回合耗时分别记录；升降档证据见dispatch_events.jsonl及scheduler_state.json。',
        '6小时为目标；滚动终态速率和预计剩余时间见progress.json，不能由短时生成吞吐推算整批完成时间。']
    for directory in batch.HISTORY:
        old = read(directory/'results.json')
        lines.append(f'{directory.name}：历史Luna每原题{old["tokens_per_original"]}，每通过版{old["tokens_per_accepted"]}；样本不同，主调度单列。')
    batch.atomic(RUN/'report_zh.md','\n\n'.join(lines)+'\n')
    from rubric_luna_bank_v1 import guidance
    guidance()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['preflight','run','finalize'])
    parser.add_argument('--acceptance',type=Path)
    args = parser.parse_args()
    batch.configure()
    with legacy.locked(RUN/'.runner.lock',blocking=False):
        manifest,_ = legacy.execution_manifest(RUN)
        legacy.verify(manifest)
        evidence = reconcile_requests()
        audit = batch.historical_audit()
        assert len(manifest['tasks'])==3000 and not set(audit['ids']) & {t['task_id'] for t in manifest['tasks']}
        for name,sha in manifest['source_file_hashes'].items():
            assert digest(batch.DATA/(name+'.jsonl'))==sha
        if args.action=='preflight':
            print(json.dumps(evidence),flush=True)
            return
        if args.action=='run':
            if not args.acceptance:
                parser.error('run requires a passing offline acceptance artifact')
            acceptance = read(args.acceptance)
            assert acceptance['passed'] and acceptance['paid_calls']==0
            assert all(digest(p)==sha for p,sha in acceptance['implementation_hashes'].items())
            manifest = freeze_execution(args.acceptance)
            legacy.verify(manifest)
            snapshot(RUN/'production_preflight.json',evidence)
            try:
                asyncio.run(Production(manifest).run())
            except BaseException:
                report(manifest)
                raise
        result = report(manifest)
        if result['completed']:
            from rubric_luna_bank_v1 import publish
            bank = publish()
            snapshot(RUN/'completion.json',dict(at=now(),all_frozen_tasks_terminal=True,
                terminal_count=result['terminal_count'],passed=result['final_passed'],
                manifest_sha256=digest(RUN/'manifest.json'),bank_manifest_ref=str(REGISTRY/'bank_manifest.json'),
                bank_manifest_sha256=digest(REGISTRY/'bank_manifest.json'),bank=bank))
        else:
            print(json.dumps(dict(stopped=True,partial=True,terminal=result['terminal_count'],
                progress_ref=str(RUN/'progress.json'))),flush=True)


if __name__=='__main__':
    main()
