"""Persistent, parameterized Luna pipeline. Only deterministic checks/accounting.

Historical preparation/compiler and single-revision state machine are reused
without changing the frozen business prompts or inspecting rubric semantics.
"""
import argparse
import asyncio
import collections
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import traceback

import rubric_luna100 as base
from rubric_candidates_100 import prepare_input

ROOT = base.ROOT
MODEL, EFFORT = base.MODEL, base.EFFORT
RUN_ID = 'rubric_luna_high_2900_batch02'
RUN = ROOT/'runs'/RUN_ID
REGISTRY = ROOT/'runs/rubric_luna_registry'
BASELINE = ROOT/'runs/rubric_luna_high_100_20261024'
PROMPTS = BASELINE/'prompts'
DATA = base.DATA
DATASET_ID = 'awm-dde80a0'
SIZE, SEED = 2900, 20261025
STARTUP_PROMPT = ROOT/'configs/rubric/luna_high_2900/run_2900.txt'
TERMINAL = {'passed', 'rejected', 'preparation_failed', 'invalid_generation', 'invalid_judge', 'call_failed', 'usage_unknown'}
read, digest, now = base.read, base.digest, base.now
RAW_PROCESS = base.process


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def snapshot(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2))


def freeze(path, value, raw=False):
    path = Path(path)
    content = value if raw else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if path.exists():
        if path.read_text() != content:
            raise ValueError('frozen_file_changed:'+str(path))
    else:
        atomic(path, content)
    return path


@contextmanager
def locked(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


REGISTRY_CACHE = {}


def replay():
    batches, tasks = {}, {}
    path = REGISTRY/'events.jsonl'
    if path.exists():
        stat = path.stat()
        cached = REGISTRY_CACHE.get(str(path))
        if cached and cached[0] == (stat.st_size, stat.st_mtime_ns):
            return cached[1].copy(), cached[2].copy()
        # A crash may leave a partial final append. Preserve it before recovery.
        data = path.read_bytes()
        if data and not data.endswith(b'\n'):
            valid, _, tail = data.rpartition(b'\n')
            freeze(REGISTRY/('partial_event_'+hashlib.sha256(tail).hexdigest()+'.bin'), tail.decode(errors='replace'), raw=True)
            atomic(path, (valid+b'\n').decode() if valid else '')
        stat = path.stat()
        cached = REGISTRY_CACHE.get(str(path))
        if cached and cached[0] == (stat.st_size, stat.st_mtime_ns):
            return cached[1].copy(), cached[2].copy()
        for line in path.read_text().splitlines():
            e = json.loads(line)
            for b in e['batches']:
                batches[b['run_id']] = b
            for t in e['tasks']:
                tasks[(t['dataset_id'], t['task_id'], t['run_id'])] = t
    if path.exists():
        stat = path.stat()
        REGISTRY_CACHE[str(path)] = ((stat.st_size, stat.st_mtime_ns), batches.copy(), tasks.copy())
    return batches, tasks


def commit(reason, batches=(), tasks=()):
    old_b, old_t = replay()
    changed_b = [b for b in batches if old_b.get(b['run_id']) != b]
    changed_t = [t for t in tasks if old_t.get((t['dataset_id'], t['task_id'], t['run_id'])) != t]
    if changed_b or changed_t:
        event = dict(at=now(), reason=reason, batches=changed_b, tasks=changed_t)
        with (REGISTRY/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(',', ':'))+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        old_b.update({b['run_id']: b for b in changed_b})
        old_t.update({(t['dataset_id'], t['task_id'], t['run_id']): t for t in changed_t})
    event_path = REGISTRY/'events.jsonl'
    if event_path.exists():
        stat = event_path.stat()
        REGISTRY_CACHE[str(event_path)] = ((stat.st_size, stat.st_mtime_ns), old_b.copy(), old_t.copy())
    snapshot(REGISTRY/'batches.json', old_b)
    atomic(REGISTRY/'tasks.jsonl', ''.join(json.dumps(t, ensure_ascii=False, separators=(',', ':'))+'\n' for _, t in sorted(old_t.items())))
    return old_b, old_t


def execution_manifest(directory):
    chain = [directory/'manifest.json'] + sorted(directory.glob('execution_manifest_v*.json'), key=lambda p:int(p.stem.rsplit('v', 1)[1]))
    initial = read(chain[0])
    for i, path in enumerate(chain[1:], 1):
        m = read(path)
        assert m['original_frozen_manifest_ref'] == str(chain[0])
        assert m['original_frozen_manifest_sha256'] == digest(chain[0])
        assert m['tasks'] == initial['tasks']
        for key in ('sample_size', 'source_file_hashes', 'prompt_hashes', 'model', 'reasoning_effort', 'sampling'):
            assert m[key] == initial[key], (path, key)
        if i > 1:
            assert m['previous_execution_manifest_ref'] == str(chain[i-1])
    return read(chain[-1]), [dict(path=str(p), sha256=digest(p)) for p in chain]


def verify(m, directory= None, implementations=True):
    directory = directory or RUN
    assert len(m['tasks']) == m['sample_size']
    assert len({t['task_id'] for t in m['tasks']}) == m['sample_size']
    if implementations:
        for path, sha in m['implementation_hashes'].items():
            assert digest(path) == sha, 'implementation_hash_mismatch:'+path
    for name, sha in m['prompt_hashes'].items():
        assert digest(directory/'prompts'/name) == sha
    for t in m['tasks']:
        for name in ('original', 'view', 'audit'):
            if name+'_ref' in t:
                assert digest(t[name+'_ref']) == t[name+'_sha256']
        for ref in t['raw_sources'].values():
            assert digest(ref['path']) == ref['sha256']
        src = read(t.get('original_ref',t.get('retained_task_ref')))
        assert hashlib.sha256(src['task'].encode()).hexdigest() == t['task_sha256']


def task_record(m, t, directory, run_id, state=None):
    folder = directory/'tasks'/t['task_id'].replace(':','__')
    state = state or (read(folder/'status.json') if (folder/'status.json').exists() else {})
    stage_refs = sorted(folder.glob('round_*/*/identity.json'), key=lambda p: list(base.STAGES).index(read(p)['stage']))
    completed_stages = [p for p in stage_refs if (p.parent/'completed.json').exists() and read(p.parent/'completed.json')['status']=='completed']
    usage_refs = sorted(folder.glob('round_*/*/usage.json'))
    rounds = state.get('rounds', [])
    status = state.get('status', 'running' if stage_refs else 'reserved')
    if status == 'source_preprocessing_failed':
        status = 'preparation_failed'
    called = False
    for p in usage_refs:
        u = read(p)
        called |= bool(u.get('response_usage') or u.get('actual_usage'))
    unknown = [str(p) for p in usage_refs if read(p).get('usage_unknown')]
    active = any((p.parent/'started.json').exists() and not (p.parent/'usage.json').exists() for p in stage_refs)
    if unknown:
        status = 'usage_unknown'
    final_revision = state.get('accepted_revision', len(rounds)-1 if rounds else None)
    return dict(dataset_id=m.get('dataset_id', DATASET_ID), dataset_sha256=m['source_file_hashes']['gen_tasks'],
                task_id=t['task_id'], scenario=t['scenario'], index=t['index'], task_sha256=t['task_sha256'],
                source_hashes={k:v['sha256'] for k,v in t['raw_sources'].items()}, run_id=run_id,
                status=status, last_completed_stage=read(completed_stages[-1]).get('stage') if completed_stages else ('prepared' if t['preparation_status']=='ready' else 'preparation_failed'),
                model_call_occurred=True if called else (None if unknown or active else False), revision_count=max(0,len(rounds)-1),
                first_judge_ref=str(folder/'round_0/judge/output.json') if (folder/'round_0/judge/output.json').exists() else None,
                final_judge_ref=str(folder/'round_1/judge/output.json') if (folder/'round_1/judge/output.json').exists() else None,
                final_version=final_revision, final_rubric_ref=str(folder/f'round_{final_revision}/rubric.json') if final_revision is not None and (folder/f'round_{final_revision}/rubric.json').exists() else None,
                accepted_ref=str(directory/'accepted'/(t['task_id'].replace(':','__')+'.json')) if status=='passed' else None,
                state_ref=str(folder/'status.json') if (folder/'status.json').exists() else None,
                errors=[dict(revision=r.get('revision'), status=r.get('status'), error=r.get('error'), validation_ref=str(folder/f"round_{r.get('revision')}/validation.json")) for r in rounds if r.get('status') not in ('passed',)],
                actual_usage_refs=list(map(str,usage_refs)), unknown_usage_refs=unknown,
                original_ref=t.get('original_ref',t.get('retained_task_ref')),
                updated_at=(folder/'status.json').stat().st_mtime_ns if (folder/'status.json').exists() else m['created_at'])


def usage_summary(entries):
    known, unknown, seen, responses = [], [], set(), {}
    for u in entries:
        tid = u.get('thread_id')
        if tid:
            if tid in seen:
                raise ValueError('duplicate_thread_usage:'+tid)
            seen.add(tid)
        if u.get('usage_unknown') or not u.get('actual_usage'):
            unknown.append(u)
            continue
        values = u['actual_usage']
        assert values['input_tokens']+values['output_tokens'] == values['total_tokens']
        for rid, val in u.get('response_usage',{}).items():
            assert rid not in responses, 'duplicate_response_id'
            responses[rid] = val
        if u.get('response_usage'):
            assert all(sum(v.get(k,0) for v in u['response_usage'].values()) == values.get(k,0) for k in base.USAGE_KEYS), 'response_cumulative_disagreement'
        known.append(u)
    totals = {k:sum(u['actual_usage'].get(k,0) for u in known) for k in base.USAGE_KEYS}
    totals['noncached_input_tokens'] = totals['input_tokens']-totals['cached_input_tokens']
    return dict(totals=totals, threads=entries, unknown_usage=unknown, unique_response_count=len(responses),
                model_response_count=len(responses), tool_call_count=sum(u.get('tool_call_count',0) for u in entries),
                cache_subset_of_input=True, reasoning_subset_of_output=True, scheduler_usage_excluded=True,
                stages={s:dict(threads=sum(u.get('stage')==s for u in entries),
                    **{k:sum(u['actual_usage'].get(k,0) for u in known if u.get('stage')==s) for k in base.USAGE_KEYS},
                    noncached_input_tokens=sum(u['actual_usage']['input_tokens']-u['actual_usage'].get('cached_input_tokens',0) for u in known if u.get('stage')==s),
                    model_response_count=sum(u.get('model_response_count',0) for u in entries if u.get('stage')==s),
                    tool_call_count=sum(u.get('tool_call_count',0) for u in entries if u.get('stage')==s),
                    unknown_usage_calls=sum(bool(u.get('usage_unknown')) for u in entries if u.get('stage')==s)) for s in base.STAGES+('model_preflight',)})


def import_history():
    m, chain = execution_manifest(BASELINE)
    verify(m, BASELINE, implementations=False)
    result, retention, accepted, usage, scheduler = (read(BASELINE/name) for name in ('results.json','retention_manifest.json','accepted_manifest.json','usage.json','scheduler_usage.json'))
    report_text = (BASELINE/'report_zh.md').read_text()
    ids = {t['task_id'] for t in m['tasks']}
    states = {s['task_id']:s for s in result['tasks']}
    assert ids == set(states) == {t['task_id'] for t in retention['tasks']}
    assert retention['frozen_manifest_sha256'] == digest(BASELINE/'manifest.json')
    assert {t['task_id'] for t in accepted} == {k for k,s in states.items() if s['status']=='passed'}
    for t in accepted:
        assert Path(t['path']).exists()
    calculated = usage_summary(usage['threads'])
    assert calculated['totals'] == usage['totals'] == result['totals']
    for u in usage['threads']:
        actual = base.thread_usage(u['thread_id'])
        assert actual['actual_config_verified']
        assert actual['actual_usage'] == u['actual_usage']
    assert result['completed'] and result['denominator']==len(ids)
    for t in m['tasks']:
        assert read(BASELINE/'tasks'/t['task_id'].replace(':','__')/'status.json') == states[t['task_id']]
    clues = dict(sample_size=100, first_passed=72, final_passed=91, revision_tasks=28, revision_additional_passed=19, luna_tokens=9666770, scheduler_snapshot_tokens=7184172)
    actual = dict(sample_size=m['sample_size'], **{k:result[k] for k in ('first_passed','final_passed','revision_tasks','revision_additional_passed')}, luna_tokens=usage['totals']['total_tokens'], scheduler_snapshot_tokens=scheduler['actual_usage']['total_tokens'])
    differences = {k:dict(expected=v,actual=actual[k]) for k,v in clues.items() if actual[k]!=v}
    if differences:
        raise ValueError('historical_accounting_discrepancies:'+json.dumps(differences))
    timestamps = [read(p) for p in BASELINE.glob('tasks/*/round_*/*/completed.json')]
    starts = [read(p)['started_at'] for p in BASELINE.glob('tasks/*/round_*/*/started.json')]
    bid = BASELINE.name
    b = dict(run_id=bid, dataset_id=DATASET_ID, source_file_hashes=m['source_file_hashes'], model=m['model'], effort=m['reasoning_effort'], seed=m['sampling']['seed'], sample_size=m['sample_size'], status='completed',
             started_at=min(starts), finished_at=max(t['finished_at'] for t in timestamps), manifest_ref=str(BASELINE/'manifest.json'), manifest_sha256=digest(BASELINE/'manifest.json'), execution_chain=chain,
             report_ref=str(BASELINE/'report_zh.md'), usage_ref=str(BASELINE/'usage.json'), luna_actual_usage=usage['totals'], scheduler_usage_ref=str(BASELINE/'scheduler_usage.json'), scheduler_snapshot_at=scheduler['read_at'], scheduler_actual_usage=scheduler['actual_usage'],
             unknown_usage=usage['unknown_usage'], discrepancies=differences, final_passed=result['final_passed'], historical_import=True,
             record_hashes={p.name:digest(p) for p in [BASELINE/x for x in ('results.json','retention_manifest.json','accepted_manifest.json','usage.json','scheduler_usage.json','report_zh.md')]})
    records = [task_record(m,t,BASELINE,bid,states[t['task_id']]) for t in m['tasks']]
    with locked(REGISTRY/'.lock'):
        commit('离线幂等导入历史全部题目；不重调模型、不重复累计用量', [b], records)
    return m


def load_assets():
    assets = {}
    for name in ('gen_tasks','gen_spec','gen_db'):
        rows = {}
        with (DATA/(name+'.jsonl')).open() as stream:
            for line in stream:
                obj = json.loads(line)
                assert obj['scenario'] not in rows, 'duplicate_scenario'
                rows[obj['scenario']] = (obj,line.rstrip('\n'))
        assets[name] = rows
    return assets


def verify_registered_batches(batches, registered):
    """Reconcile all prior batches without rewriting their artifacts or calling models."""
    for bid, batch in batches.items():
        if bid == RUN_ID:
            continue
        directory = Path(batch['manifest_ref']).parent
        manifest, chain = execution_manifest(directory)
        verify(manifest, directory, implementations=False)
        assert digest(directory/'manifest.json') == batch['manifest_sha256']
        assert chain == batch['execution_chain']
        records = {t['task_id']:t for t in registered.values() if t['run_id']==bid}
        assert set(records) == {t['task_id'] for t in manifest['tasks']}
        assert len(records) == batch['sample_size']
        for task in manifest['tasks']:
            assert records[task['task_id']]['task_sha256'] == task['task_sha256']
        if batch['status'] != 'completed':
            continue  # All reservations still participate in exclusion.
        result = read(directory/'results.json')
        states = {t['task_id']:t for t in result['tasks']}
        assert set(states) == set(records)
        assert result['completed'] and result['denominator'] == len(records)
        retention = read(directory/'retention_manifest.json')
        assert retention['frozen_manifest_sha256'] == batch['manifest_sha256']
        assert {t['task_id'] for t in retention['tasks']} == set(records)
        for tid, state in states.items():
            assert records[tid]['status'] == state['status']
            assert read(directory/'tasks'/tid.replace(':','__')/'status.json') == state
        accepted = read(directory/'accepted_manifest.json')
        assert {t['task_id'] for t in accepted} == {tid for tid,s in states.items() if s['status']=='passed'}
        for item in accepted:
            if 'sha256' in item:
                assert digest(item['path']) == item['sha256']
            else:
                # The original 100-task index predates artifact hashes.
                state=states[item['task_id']]
                source=directory/'tasks'/item['task_id'].replace(':','__')/f"round_{state['accepted_revision']}"/'rubric.json'
                assert Path(item['path']).read_bytes() == source.read_bytes()
        usage = read(directory/'usage.json')
        assert usage_summary(usage['threads'])['totals'] == usage['totals'] == result['totals'] == batch['luna_actual_usage']
        saved = {u['thread_id']:u for u in usage['threads'] if u.get('task_id') is not None}
        actual = {read(p)['thread_id']:read(p) for p in directory.glob('tasks/*/round_*/*/usage.json')}
        assert saved == actual


def prepare():
    historical = import_history()
    with locked(REGISTRY/'.lock'):
        batches, registered = replay()
        verify_registered_batches(batches, registered)
        if (RUN/'manifest.json').exists():
            m, _ = execution_manifest(RUN)
            assert m['run_id']==RUN_ID and m['sample_size']==SIZE and m['sampling']['seed']==SEED
            verify(m)
            assert digest(STARTUP_PROMPT)==m['prompt_hashes'][STARTUP_PROMPT.name], 'startup_prompt_changed'
            records = [task_record(m,t,RUN,RUN_ID) for t in m['tasks']]
            for t in records:
                assert not any(x['dataset_id']==DATASET_ID and x['task_id']==t['task_id'] and x['run_id']!=RUN_ID for x in registered.values())
            commit('从原冻结manifest恢复占位', [batch_record(m,batches.get(RUN_ID))], records)
            return m
        assets = load_assets()
        hashes = {name:digest(DATA/(name+'.jsonl')) for name in assets}
        population = sorted(f'{s}:{i}' for s,(r,_) in assets['gen_tasks'].items() for i in range(len(r['tasks'])))
        assert len(population)==len(set(population))
        excluded = sorted({t['task_id'] for t in registered.values() if t['dataset_id']==DATASET_ID})
        for t in registered.values():
            if t['dataset_id']!=DATASET_ID:
                raise ValueError('unexpected_dataset_identity_in_series')
            scenario, index = t['task_id'].rsplit(':',1)
            current = assets['gen_tasks'][scenario][0]['tasks'][int(index)]
            assert hashlib.sha256(current.encode()).hexdigest()==t['task_sha256'], 'registered_task_changed'
        # Changed dataset hashes must be explicit, never quietly resampled.
        if hashes != historical['source_file_hashes']:
            raise ValueError('dataset_hash_changed_requires_explicit_reconciliation')
        eligible = sorted(set(population)-set(excluded))
        if len(eligible)<SIZE:
            raise ValueError('insufficient_new_tasks')
        selection = random.Random(SEED).sample(eligible,SIZE)
        freeze(RUN/'population.json', population)
        freeze(RUN/'excluded.json', excluded)
        freeze(RUN/'eligible.json', eligible)
        freeze(RUN/'selection.json', selection)
        rows=[]
        for task_id in selection:
            scenario, idx = task_id.rsplit(':',1)
            index=int(idx)
            task=assets['gen_tasks'][scenario][0]['tasks'][index]
            folder=RUN/'inputs'/task_id.replace(':','__')
            raw_refs={}
            try:
                for name in assets:
                    path=freeze(RUN/'sources'/scenario/(name+'.json'),assets[name][scenario][1],raw=True)
                    raw_refs[name]=dict(path=str(path),sha256=digest(path))
                spec=assets['gen_spec'][scenario][0]
                endpoints=[e for g in spec['api_spec']['api_groups'] for e in g['endpoints']]
                sources=[dict(id='task',kind='task',text=task),dict(id='contract:'+scenario,kind='public_contract',text=assets['gen_spec'][scenario][1])]
                for table in assets['gen_db'][scenario][0]['db_schema']['tables']:
                    sources.append(dict(id='schema:'+table['name'],kind='schema',text=table['ddl']+'\n'+'\n'.join(table.get('indexes',[]))))
                original=dict(task_id=task_id,task=task,sources=sources,
                    tool_inventory=[dict(operation_id=e.get('operation_id'),method=e.get('method'),path=e.get('path')) for e in endpoints],
                    available_evidence_types=['执行前后数据库快照（可按公开DDL查询）','工具参数、真实返回、事件顺序及时间','最终答复'],
                    evidence_note='这里只给出将来可收集的证据类型，没有初态、时钟或真实执行值；动作中间状态不保证可重建。',quality_policy=None)
                freeze(folder/'original.json',original)
                view,audit=prepare_input(original)
                freeze(folder/'view.json',view)
                freeze(folder/'audit.json',audit)
                row=dict(task_id=task_id,scenario=scenario,index=index,preparation_status='ready',raw_sources=raw_refs)
                for name in ('original','view','audit'):
                    path=folder/(name+'.json')
                    row[name+'_ref']=str(path)
                    row[name+'_sha256']=digest(path)
            except Exception as exc:
                path=freeze(folder/'retained_task.json',dict(task_id=task_id,task=task,raw_sources=raw_refs))
                row=dict(task_id=task_id,scenario=scenario,index=index,preparation_status='preparation_failed',error=repr(exc),raw_sources=raw_refs,retained_task_ref=str(path))
            row['task_sha256']=hashlib.sha256(task.encode()).hexdigest()
            rows.append(row)
        for name in ('generate','judge','revise','execute','id_rules'):
            freeze(RUN/'prompts'/(name+'.txt'),(PROMPTS/(name+'.txt')).read_text(),raw=True)
            assert digest(RUN/'prompts'/(name+'.txt'))==digest(PROMPTS/(name+'.txt'))
        freeze(RUN/'prompts'/STARTUP_PROMPT.name,STARTUP_PROMPT.read_text(),raw=True)
        files=[Path(__file__),ROOT/'scripts/rubric_luna100.py',ROOT/'scripts/rubric_candidates_100.py',ROOT/'scripts/rubric_candidates.py',ROOT/'scripts/rubric_pilot.py',ROOT/'scripts/rubric_luna_batch.py',ROOT/'tests/test_rubric_luna_batch.py',ROOT/'tests/test_rubric_luna_batch02.py']
        m=dict(version='luna-batch-v1',run_id=RUN_ID,dataset_id=DATASET_ID,created_at=now(),model=MODEL,reasoning_effort=EFFORT,tasks=rows,sample_size=len(rows),population_size=len(population),eligible_size=len(eligible),
            sampling=dict(method='random.Random(seed).sample(sorted(eligible_task_ids),sample_size)',seed=SEED),
            population_snapshots={n:dict(path=str(RUN/(n+'.json')),sha256=digest(RUN/(n+'.json'))) for n in ('population','excluded','eligible','selection')},
            source_file_hashes=hashes,prompt_hashes={p.name:digest(p) for p in (RUN/'prompts').glob('*.txt')},implementation_hashes={str(p):digest(p) for p in files},
            concurrency=3,fork_turns='none',context_mechanism='new codex exec session; one message per task and stage',max_external_revisions=1,automatic_retries=0,user_context_cap=None,cumulative_token_budget=None,
            semantic_review_by_coordinator=False,pass_rule='five valid integer scores in 0/1/2; none zero',
            source_method=historical['source_method'],excluded_data=historical['excluded_data'],baseline_ref=str(BASELINE),registry_ref=str(REGISTRY),prompt_differences=[],source_method_differences=[])
        freeze(RUN/'manifest.json',m)
        commit('冻结新增样本；全部占位后才允许模型调用',[batch_record(m)],[task_record(m,t,RUN,RUN_ID) for t in rows])
        cumulative_report()
        return m


def batch_record(m, previous=None, result=None, usage=None):
    b=dict(previous or {})
    b.update(run_id=RUN_ID,dataset_id=DATASET_ID,source_file_hashes=m['source_file_hashes'],model=MODEL,effort=EFFORT,seed=m['sampling']['seed'],sample_size=m['sample_size'],
        manifest_ref=str(RUN/'manifest.json'),manifest_sha256=digest(RUN/'manifest.json'),execution_chain=execution_manifest(RUN)[1],report_ref=str(RUN/'report_zh.md'),usage_ref=str(RUN/'usage.json'))
    b.setdefault('status','reserved');b.setdefault('started_at',None);b.setdefault('finished_at',None)
    b.setdefault('luna_actual_usage',None);b.setdefault('unknown_usage',[])
    if result:
        b.update(status='completed' if result['completed'] else 'partial',final_passed=result['final_passed'])
    if usage:
        b.update(luna_actual_usage=usage['totals'],unknown_usage=usage['unknown_usage'])
    if (RUN/'scheduler_usage.json').exists():
        s=read(RUN/'scheduler_usage.json')
        b.update(scheduler_usage_ref=str(RUN/'scheduler_usage.json'),scheduler_snapshot_at=s['read_at'],scheduler_actual_usage=s.get('actual_usage'))
    return b


def registry_task(task, reason):
    m=MANIFEST_CACHE or read(RUN/'manifest.json')
    record=task_record(m,task,RUN,RUN_ID)
    with locked(REGISTRY/'.lock'):
        commit(reason,tasks=[record])


def stage_for(role,revision):
    return ('initial_judge' if revision==0 else 'fresh_final_judge') if role=='judge' else ('initial_generation' if revision==0 else 'one_revision_generation')


def parse_events(folder):
    tid=None;error=False;done=False
    path=folder/'events.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            if e.get('type')=='thread.started':tid=e.get('thread_id')
            if e.get('type')=='turn.completed':done=True
            if e.get('type') in ('error','turn.failed'):
                msg=json.dumps(e).lower()
                error |= any(s in msg for s in ('model_not_found','model is not supported','model does not exist','unsupported model','not available for'))
    return tid,error,done


def recover_call(folder,identity):
    tid,unavailable,done=parse_events(folder)
    if unavailable:
        raise base.ModelUnavailable('required_model_unavailable')
    if (folder/'completed.json').exists():
        c=read(folder/'completed.json')
        assert c['identity']==identity
        if c['status']=='model_unavailable':raise base.ModelUnavailable(c['status'])
        if c['status']!='completed':raise base.CallFailure(c['status'])
        return (folder/'response.txt').read_text()
    if not (folder/'started.json').exists():return None
    # A persisted completed CLI event and raw output prove safe offline recovery.
    usage=base.thread_usage(tid) if tid else dict(usage_unknown=True,error='no_thread_id_after_launch')
    freeze(folder/'usage.json',{**identity,**usage})
    if done and (folder/'response.txt').exists() and usage.get('actual_config_verified'):
        freeze(folder/'completed.json',dict(identity=identity,status='completed',thread_id=tid,finished_at=now(),recovered_offline=True))
        return (folder/'response.txt').read_text()
    raise base.CallFailure('usage_unknown_interrupted_call_no_automatic_retry')


async def call(role,revision,task_id,payload,sem):
    stage=stage_for(role,revision)
    folder=RUN/'tasks'/task_id.replace(':','__')/f'round_{revision}'/role
    pack=dict(role=role,execution=(RUN/'prompts/execute.txt').read_text(),protocol=(RUN/'prompts'/(role+'.txt')).read_text(),data=payload)
    freeze(folder/'input.json',pack)
    identity=dict(run_id=RUN_ID,task_id=task_id,stage=stage,revision=revision,model=MODEL,reasoning_effort=EFFORT,input_sha256=digest(folder/'input.json'),prompt_sha256=digest(RUN/'prompts'/(role+'.txt')),execution_sha256=digest(RUN/'prompts/execute.txt'),fork_turns='none')
    freeze(folder/'identity.json',identity)
    cached=recover_call(folder,identity)
    if cached is not None:return cached
    message='按以下完整输入包的execution和protocol处理单题单阶段。使用消息模式，只返回业务JSON，不调用工具，不读取文件，不启动子agent。\n'+json.dumps(pack,ensure_ascii=False,separators=(',',':'))
    task=TASKS[task_id]
    async with sem:
        freeze(folder/'started.json',dict(identity=identity,started_at=now()))
        registry_task(task,'阶段开始；持久化请求身份')
        cmd=['codex','exec','--skip-git-repo-check','--ignore-user-config','-m',MODEL,'-c','model_reasoning_effort="high"','-s','read-only','--json','-o',str(folder/'response.txt'),'-']
        with (folder/'events.jsonl').open('xb') as out,(folder/'stderr.txt').open('xb') as err:
            try:
                proc=await asyncio.create_subprocess_exec(*cmd,stdin=asyncio.subprocess.PIPE,stdout=out,stderr=err,cwd=str(ROOT))
                await proc.communicate(message.encode())
            except OSError as exc:
                freeze(folder/'launcher_not_started.json',dict(error=repr(exc),verified_no_model_call=True,at=now()))
                raise base.CallFailure('launcher_not_started:'+repr(exc))
        tid,unavailable,done=parse_events(folder)
        usage=base.thread_usage(tid) if tid else dict(usage_unknown=True,error='launcher_created_no_known_thread')
        freeze(folder/'usage.json',{**identity,**usage})
        mismatch=usage.get('model') and not usage.get('actual_config_verified')
        success=proc.returncode==0 and done and (folder/'response.txt').exists() and usage.get('actual_config_verified')
        status='completed' if success else ('model_unavailable' if unavailable or mismatch else 'call_failed')
        freeze(folder/'completed.json',dict(identity=identity,status=status,returncode=proc.returncode,thread_id=tid,finished_at=now()))
        registry_task(task,'阶段原始响应和实际用量已持久化')
        if unavailable or mismatch:raise base.ModelUnavailable('required_model_configuration_unavailable_or_mismatched')
        if not success:raise base.CallFailure(status)
        return (folder/'response.txt').read_text()


TASKS={}
MANIFEST_CACHE={}


def configure():
    base.RUN=RUN
    base.freeze=freeze
    base.snapshot=snapshot
    base.call=call


async def process(task,sem):
    folder=RUN/'tasks'/task['task_id'].replace(':','__')
    if (folder/'status.json').exists():
        registry_task(task,'恢复已有终态；不重调模型')
        return read(folder/'status.json')
    try:
        state=await RAW_PROCESS(task,sem)
    except base.ModelUnavailable:
        raise
    except Exception as exc:
        # Isolate unexpected deterministic/IO errors; retain all existing calls.
        snapshot(folder/'runner_error.json',dict(error=repr(exc),traceback=traceback.format_exc(),at=now()))
        stage_usages=[read(p) for p in folder.glob('round_*/*/usage.json')]
        state=dict(task_id=task['task_id'],status='usage_unknown' if any(u.get('usage_unknown') for u in stage_usages) else 'call_failed',error=repr(exc),rounds=[],task_retained=True)
        freeze(folder/'status.json',state)
    if any(read(p).get('usage_unknown') for p in folder.glob('round_*/*/usage.json')):
        state['status']='usage_unknown'
        snapshot(folder/'status.json',state)
    registry_task(task,'逐题终态已保存')
    return state


def progress(m,states):
    return dict(frozen_tasks=m['sample_size'],completed=len(states),remaining=m['sample_size']-len(states),statuses=dict(collections.Counter(s['status'] for s in states.values())),updated_at=now())


def scheduler_snapshot(final=False):
    tid=os.environ.get('CODEX_THREAD_ID')
    if not tid:return
    usage=base.thread_usage(tid)
    snapshot(RUN/'scheduler_usage.json',dict(**usage,read_at=now(),accounting='本批主调度线程累计快照；不包含快照之后回复；与Luna分账',final_snapshot=final))


def report(m):
    states={s['task_id']:s for s in (read(p) for p in (RUN/'tasks').glob('*/status.json'))}
    assert set(states)<=set(TASKS)
    usage=usage_summary([read(p) for p in RUN.glob('tasks/*/round_*/*/usage.json')]+[read(p) for p in RUN.glob('preflight/*/usage.json')])
    usage['read_at']=now()
    snapshot(RUN/'usage.json',usage)
    passed=sum(s['status']=='passed' for s in states.values())
    completed=len(states)==m['sample_size']
    result=dict(completed=completed,denominator=m['sample_size'],tasks=list(states.values()),first_passed=sum(s.get('first_status')=='passed' for s in states.values()),final_passed=passed,
        revision_tasks=sum(s.get('entered_revision',False) for s in states.values()),revision_additional_passed=sum(s['status']=='passed' and s.get('entered_revision',False) for s in states.values()),
        failure_types=dict(collections.Counter(s['status'] for s in states.values() if s['status']!='passed')),totals=usage['totals'],stages=usage['stages'],
        unknown_usage_calls=len(usage['unknown_usage']),tokens_per_original=usage['totals']['total_tokens']/m['sample_size'] if completed else None,
        tokens_per_accepted=usage['totals']['total_tokens']/passed if passed else None,semantic_review_by_coordinator=False)
    snapshot(RUN/'results.json',result)
    snapshot(RUN/'failures.json',[s for s in states.values() if s['status']!='passed'])
    snapshot(RUN/'progress.json',progress(m,states))
    baseline=read(BASELINE/'results.json')
    lines=[f'# Luna high 新增{m["sample_size"]}题',f'状态：{"已完成" if completed else "部分完成"}；{len(states)}/{m["sample_size"]}题取得终态。',
        f'首次通过 {result["first_passed"]}/{m["sample_size"]}；最终通过 {passed}/{m["sample_size"]}；进入修订 {result["revision_tasks"]}；修订新增通过 {result["revision_additional_passed"]}。',
        '失败分类：'+json.dumps(result['failure_types'],ensure_ascii=False),'', '| 用量项 | 实测tokens |','|---|---:|']
    lines += [f'| {k} | {v:,} |' for k,v in usage['totals'].items()]
    lines += ['', '| 阶段 | 线程数 | tokens |','|---|---:|---:|']+[f'| {s} | {u["threads"]} | {u["total_tokens"]:,} |' for s,u in usage['stages'].items()]
    lines += ['',f'模型响应 {usage["model_response_count"]}；工具调用 {usage["tool_call_count"]}；未知用量项 {len(usage["unknown_usage"])}。',
        f'每道冻结原题平均：{result["tokens_per_original"]}；每份通过版平均（含失败成本）：{result["tokens_per_accepted"] if passed else "未定义"}。',
        f'历史100题Luna每题 {baseline["tokens_per_original"]}，每份通过版 {baseline["tokens_per_accepted"]}。',
        '主调度另见scheduler_usage.json，仅为记录截止时间的累计快照，不是整个chat最终账单。缓存是输入子项，思考是输出子项，不重复相加。',
        '样本排除历史1100题及其他占位后按字符串ID排序、seed=20261025无放回抽样；原题、完整公开契约与DDL、词法候选及原文offset保留。五份业务prompt逐字复用历史冻结版，无差异；本批调度提示词为run_2900.txt，准备方式未改变。',
        '程序改为参数化runner、统一锁和事件登记、原子写入、持久化阶段身份及断点恢复。旧脚本和历史产物不改写。',
        '每题每阶段为全新Luna high会话，最多一次修订且修订版必须全新复审；五维无0通过。调度没有语义复核、评分或改写。审查通过率不代表人工准确率。',
        '只运行本manifest，不追加其余题库、不重跑历史失败题。未知用量不得解释为0。']
    atomic(RUN/'report_zh.md','\n\n'.join(lines)+'\n')
    comparison=[]
    for directory in (BASELINE, ROOT/'runs/rubric_luna_high_1000_batch01'):
        prior=read(directory/'results.json')
        comparison.append(f'{directory.name}：Luna每题 {prior["tokens_per_original"]}，每份通过版 {prior["tokens_per_accepted"]}；主调度单列。')
    atomic(RUN/'report_zh.md',(RUN/'report_zh.md').read_text()+'\n'.join(comparison)+'\n')
    with locked(REGISTRY/'.lock'):
        bs,_=replay()
        b=batch_record(m,bs.get(RUN_ID),result,usage)
        if completed:b['finished_at']=now()
        commit('批次离线汇总与用量核对',[b],[task_record(m,t,RUN,RUN_ID,states.get(t['task_id'])) for t in m['tasks']])
        cumulative_report()
    return result


def cumulative_report():
    batches,tasks=replay()
    seen=set()
    for t in tasks.values():
        key=(t['dataset_id'],t['task_id'])
        assert key not in seen, 'duplicate_task_across_series'
        seen.add(key)
    lines=['# Luna系列累计登记','范围：仅历史Luna high 100题及本系列新增批次；更早其他模型和10题实验不计入。', '| 批次 | 冻结 | 终态 | 通过 | Luna实测tokens |','|---|---:|---:|---:|---:|']
    total=0
    for bid,b in batches.items():
        ts=[t for t in tasks.values() if t['run_id']==bid]
        tokens=(b.get('luna_actual_usage') or {}).get('total_tokens')
        if tokens is not None:total+=tokens
        lines.append(f'| {bid} | {b["sample_size"]} | {sum(t["status"] in TERMINAL for t in ts)} | {sum(t["status"]=="passed" for t in ts)} | {tokens if tokens is not None else "未知/尚未调用"} |')
        lines.append('失败分类 '+bid+'：'+json.dumps(dict(collections.Counter(t['status'] for t in ts if t['status'] in TERMINAL-{'passed'})),ensure_ascii=False))
    lines += [f'冻结去重题数 {len(seen)}；已记录Luna实测用量合计 {total:,} tokens。',
              '各批主调度快照、截止时间、未知用量分别见batches.json；不得与Luna混用对照。全部冻结题取得终态才称累计全部完成。']
    atomic(REGISTRY/'report_zh.md','\n\n'.join(lines)+'\n')
    atomic(REGISTRY/'README.md','''# Luna系列登记

范围为历史100题和本系列新增批次，历史通过和失败全部排除出新增抽样。其他模型及早期10题实验不计入。

batches.json按run_id登记批次；tasks.jsonl每行对应(dataset_id, task_id, run_id)，区分reserved、running和终态。events.jsonl追加完整状态变更，可重建两个索引。report_zh.md为累计统计；模型用量按批次实测去重相加，导入不再计费，主调度和未知用量单列。

查询：`rg '"task_id":"需要的scenario:index"' runs/rubric_luna_registry/tasks.jsonl`。

续跑：本次2900题使用原参数执行scripts/rubric_luna_batch02.py run --sample-size 2900 --seed 20261025；历史1000题仍用其原入口scripts/rubric_luna_batch.py。完整启动参数见configs/rubric/luna_high_2900/run_2900.txt。持有批次运行锁并核对冻结manifest，已完成阶段只复用身份完全相同的原始响应，已终态题不再调用。启动后未取得可证实完整响应的请求标为未知，不自动重试。不重新抽样、不换run_id。登记缺失可从events重建；冻结manifest已写入但事件尚未写入时恢复原manifest占位。
''')


def finalize(m):
    verify(m)
    states={s['task_id']:s for s in (read(p) for p in RUN.glob('tasks/*/status.json'))}
    if set(states)!={t['task_id'] for t in m['tasks']}:return False
    retained=[];accepted=[]
    for t in m['tasks']:
        s=states[t['task_id']]
        assert len(s['rounds'])<=2
        path=RUN/'accepted'/(t['task_id'].replace(':','__')+'.json')
        assert path.exists()==(s['status']=='passed')
        retained.append(dict(task_id=t['task_id'],original_retained=True,hashes_verified=True,state=s['status']))
        if path.exists():accepted.append(dict(task_id=t['task_id'],path=str(path),sha256=digest(path)))
    snapshot(RUN/'retention_manifest.json',dict(tasks=retained,frozen_manifest_sha256=digest(RUN/'manifest.json'),semantic_review_performed=False))
    snapshot(RUN/'accepted_manifest.json',accepted)
    return True


async def run(m,workers):
    configure()
    MANIFEST_CACHE.update(m)
    TASKS.update({t['task_id']:t for t in m['tasks']})
    verify(m)
    with locked(REGISTRY/'.lock'):
        bs,_=replay();b=batch_record(m,bs[RUN_ID]);b.update(status='running',started_at=b.get('started_at') or now());commit('开始或恢复固定批次',[b])
    queue=asyncio.Queue()
    states={s['task_id']:s for s in (read(p) for p in RUN.glob('tasks/*/status.json'))}
    for t in m['tasks']:
        if t['task_id'] not in states:queue.put_nowait(t)
    sem=asyncio.Semaphore(min(workers,3));stop=asyncio.Event()
    consecutive_call_failures=0
    async def worker():
        nonlocal consecutive_call_failures
        while not queue.empty() and not stop.is_set():
            task=queue.get_nowait()
            try:
                state=await process(task,sem)
                states[task['task_id']]=state
                consecutive_call_failures = consecutive_call_failures+1 if state['status'] in ('call_failed','usage_unknown') else 0
                if consecutive_call_failures>=3:
                    stop.set()
                    snapshot(RUN/'dispatch_paused.json',dict(reason='three_consecutive_infrastructure_failures',at=now()))
            except base.ModelUnavailable as exc:
                stop.set();snapshot(RUN/'model_unavailable.json',dict(error=str(exc),task_id=task['task_id'],at=now()))
            finally:queue.task_done()
            snapshot(RUN/'progress.json',progress(m,states))
            if len(states)%50==0:
                print(json.dumps(dict(completed=len(states),denominator=m['sample_size']),ensure_ascii=False),flush=True)
    await asyncio.gather(*(worker() for _ in range(min(workers,3))))
    scheduler_snapshot(True)
    result=report(m)
    audited=finalize(m)
    snapshot(RUN/'completion.json',dict(completed=result['completed'],audited=audited,luna_total_tokens=result['totals']['total_tokens'],unknown_usage_calls=result['unknown_usage_calls'],finished_at=now(),run_id=RUN_ID))
    print(json.dumps(read(RUN/'completion.json')),flush=True)


def main():
    global RUN,RUN_ID,REGISTRY,BASELINE,PROMPTS,SIZE,SEED,DATA,DATASET_ID,STARTUP_PROMPT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','report'])
    parser.add_argument('--run-dir',type=Path,default=RUN)
    parser.add_argument('--registry-dir',type=Path,default=REGISTRY)
    parser.add_argument('--baseline-dir',type=Path,default=BASELINE)
    parser.add_argument('--prompt-dir',type=Path)
    parser.add_argument('--startup-prompt',type=Path,default=STARTUP_PROMPT)
    parser.add_argument('--data-dir',type=Path,default=DATA)
    parser.add_argument('--sample-size',type=int,default=SIZE)
    parser.add_argument('--seed',type=int,default=SEED)
    parser.add_argument('--workers',type=int,choices=[1,2,3],default=3)
    args=parser.parse_args()
    RUN=args.run_dir.resolve();RUN_ID=RUN.name;REGISTRY=args.registry_dir.resolve();BASELINE=args.baseline_dir.resolve();PROMPTS=(args.prompt_dir or BASELINE/'prompts').resolve();DATA=args.data_dir.resolve();SIZE=args.sample_size;SEED=args.seed
    STARTUP_PROMPT=args.startup_prompt.resolve()
    assert SIZE>0
    with locked(RUN/'.runner.lock',blocking=False):
        if args.action in ('prepare','run'):
            m=prepare()
        else:m=read(RUN/'manifest.json')
        TASKS.update({t['task_id']:t for t in m['tasks']})
        if args.action=='run':asyncio.run(run(m,args.workers))
        elif args.action=='report':
            scheduler_snapshot();report(m);finalize(m)
        else:print(json.dumps(dict(frozen=m['sample_size'],run_id=RUN_ID)))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        snapshot(RUN/'fatal_error.json',dict(error=repr(exc),traceback=traceback.format_exc(),at=now()))
        raise
