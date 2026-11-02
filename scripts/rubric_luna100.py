"""Frozen 100-task Luna pipeline; deterministic checks and actual usage only.

Each model invocation is a new codex exec session (never resume/fork), with a
single task/stage in message mode. No rubric semantics are reviewed here.
"""
import argparse
import asyncio
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3

from rubric_candidates_100 import prepare_input, validate_selection, compile_rubric, evaluate, json_spans

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'runs/rubric_luna_high_100_20261024'
PROMPTS = ROOT/'configs/rubric/luna_high_100'
DATA = ROOT/'data/awm-dde80a0'
MODEL = 'gpt-5.6-luna'
EFFORT = 'high'
STAGES = ('initial_generation', 'initial_judge', 'one_revision_generation', 'fresh_final_judge')
USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def manifest():
    amendments = sorted(RUN.glob('execution_manifest_v*.json'), key=lambda p: int(p.stem.rsplit('v', 1)[1]))
    return read(amendments[-1] if amendments else RUN/'manifest.json')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path, value, raw=False):
    path = Path(path)
    content = value if raw else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() != content:
            raise ValueError('frozen_file_changed:'+str(path))
    else:
        with path.open('x') as stream:
            stream.write(content)
    return path


def snapshot(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def prepare():
    if (RUN/'manifest.json').exists():
        raise ValueError('already_frozen_use_original_manifest')
    assets = {}
    for name in ('gen_tasks', 'gen_spec', 'gen_db'):
        rows = {}
        with (DATA/(name+'.jsonl')).open() as stream:
            for line in stream:
                obj = json.loads(line)
                if obj['scenario'] in rows:
                    raise ValueError('duplicate_scenario')
                rows[obj['scenario']] = (obj, line.rstrip('\n'))
        assets[name] = rows
    population = sorted((f'{scenario}:{i}', scenario, i) for scenario, (r, _) in assets['gen_tasks'].items() for i in range(len(r['tasks'])))
    if len({t[0] for t in population}) != len(population):
        raise ValueError('duplicate_task_id')
    selection = random.Random(20261024).sample(population, 100)
    tasks = []
    for task_id, scenario, index in selection:
        folder = RUN/'inputs'/task_id.replace(':', '__')
        task = assets['gen_tasks'][scenario][0]['tasks'][index]
        # Freeze the original source lines once per environment, never row data.
        raw_refs = {}
        try:
            for name in assets:
                path = freeze(RUN/'sources'/scenario/(name+'.json'), assets[name][scenario][1], raw=True)
                raw_refs[name] = dict(path=str(path), sha256=digest(path))
            spec = assets['gen_spec'][scenario][0]
            endpoints = [e for g in spec['api_spec']['api_groups'] for e in g['endpoints']]
            sources = [dict(id='task', kind='task', text=task),
                       dict(id='contract:'+scenario, kind='public_contract', text=assets['gen_spec'][scenario][1])]
            for table in assets['gen_db'][scenario][0]['db_schema']['tables']:
                ddl = table['ddl'] + '\n' + '\n'.join(table.get('indexes', []))
                sources.append(dict(id='schema:'+table['name'], kind='schema', text=ddl))
            original = dict(task_id=task_id, task=task, sources=sources,
                            tool_inventory=[dict(operation_id=e.get('operation_id'), method=e.get('method'), path=e.get('path')) for e in endpoints],
                            available_evidence_types=['执行前后数据库快照（可按公开DDL查询）', '工具参数、真实返回、事件顺序及时间', '最终答复'],
                            evidence_note='这里只给出将来可收集的证据类型，没有初态、时钟或真实执行值；动作中间状态不保证可重建。', quality_policy=None)
            freeze(folder/'original.json', original)
            view, pack = prepare_input(original)
            freeze(folder/'view.json', view)
            freeze(folder/'audit.json', pack)
            row = dict(task_id=task_id, scenario=scenario, index=index, preparation_status='ready', raw_sources=raw_refs)
            for name in ('original', 'view', 'audit'):
                path = folder/(name+'.json')
                row[name+'_ref'] = str(path)
                row[name+'_sha256'] = digest(path)
        except Exception as exc:
            freeze(folder/'retained_task.json', dict(task_id=task_id, task=task, raw_sources=raw_refs))
            row = dict(task_id=task_id, scenario=scenario, index=index, preparation_status='source_preprocessing_failed',
                       error=type(exc).__name__+':'+str(exc), raw_sources=raw_refs, retained_task_ref=str(folder/'retained_task.json'))
        row['task_sha256'] = hashlib.sha256(task.encode()).hexdigest()
        tasks.append(row)
    for path in PROMPTS.glob('*.txt'):
        freeze(RUN/'prompts'/path.name, path.read_text(), raw=True)
    files = [Path(__file__), ROOT/'scripts/rubric_candidates_100.py', ROOT/'scripts/rubric_candidates.py', ROOT/'scripts/rubric_pilot.py', ROOT/'tests/test_rubric_candidates_100.py']
    manifest = dict(version='luna100-v1', created_at=now(), model=MODEL, reasoning_effort=EFFORT,
                    tasks=tasks, sample_size=len(tasks), population_size=len(population),
                    sampling=dict(method='random.Random(seed).sample(sorted(scenario:index),100); without replacement', seed=20261024),
                    fork_turns='none', context_mechanism='new codex exec process/session for every task and stage; no resume/fork/history',
                    concurrency=3, max_external_revisions=1, user_context_cap=None, cumulative_token_budget=None,
                    automatic_retries=0, pass_rule='five valid integer scores in 0/1/2, none zero', semantic_review_by_coordinator=False,
                    source_method='complete public API spec and all table/index DDL; original raw lines frozen; only confirmed descriptor example metadata omitted from model view',
                    excluded_data=['database rows', 'private answers', 'historical rubrics', 'execution trajectories'],
                    source_file_hashes={name:digest(DATA/(name+'.jsonl')) for name in assets},
                    prompt_hashes={p.name:digest(p) for p in (RUN/'prompts').glob('*.txt')},
                    implementation_hashes={str(p):digest(p) for p in files},
                    baseline_ref=str(ROOT/'runs/rubric_luna56_lean_10_20261024/report_zh.md'), full_run=False)
    freeze(RUN/'manifest.json', manifest)
    print(json.dumps(dict(prepared=len(tasks), ready=sum(t['preparation_status']=='ready' for t in tasks), run=str(RUN))))


def verify_manifest(m):
    for path, h in m['implementation_hashes'].items():
        if digest(path) != h:
            raise ValueError('implementation_hash_mismatch:'+path)
    for name, h in m['prompt_hashes'].items():
        if digest(RUN/'prompts'/name) != h:
            raise ValueError('prompt_hash_mismatch:'+name)
    for task in m['tasks']:
        for name in ('original', 'view', 'audit'):
            if name+'_ref' in task and digest(task[name+'_ref']) != task[name+'_sha256']:
                raise ValueError('input_hash_mismatch:'+task['task_id'])


def thread_usage(thread_id):
    with sqlite3.connect('file:/home/bjx/.codex/state_5.sqlite?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        r = db.execute('select id,model,reasoning_effort,rollout_path from threads where id=?', (thread_id,)).fetchone()
    if r is None:
        return dict(thread_id=thread_id, usage_unknown=True, error='missing_thread_metadata')
    responses, contexts, cumulative, calls = {}, [], None, []
    for line in Path(r['rollout_path']).open():
        d = json.loads(line)
        p = d.get('payload', {})
        if d['type'] == 'token_usage_record':
            rid = p['response_id']
            if rid in responses and responses[rid] != p['usage']:
                raise ValueError('response_usage_conflict')
            responses[rid] = p['usage']
        if d['type'] == 'turn_context':
            contexts.append(dict(model=p.get('model'), effort=p.get('effort')))
        if d['type'] == 'event_msg' and p.get('type') == 'token_count' and p.get('info'):
            cumulative = p['info'].get('total_token_usage') or cumulative
        if d['type'] == 'response_item' and p.get('type') in ('function_call', 'custom_tool_call', 'web_search_call'):
            calls.append(dict(call_id=p.get('call_id', p.get('id')), name=p.get('name', p.get('type'))))
    actual_config = r['model'] == MODEL and r['reasoning_effort'] == EFFORT and bool(contexts) and all(c == dict(model=MODEL, effort=EFFORT) for c in contexts)
    totals = {k: sum(u.get(k, 0) for u in responses.values()) for k in USAGE_KEYS}
    matches = bool(cumulative) and all(totals[k] == cumulative.get(k, 0) for k in USAGE_KEYS)
    return dict(thread_id=thread_id, model=r['model'], reasoning_effort=r['reasoning_effort'], actual_config_verified=actual_config,
                actual_usage=cumulative or (totals if responses else None), response_usage=responses, usage_unknown=not bool(cumulative or responses),
                model_response_count=len(responses), tool_call_count=len(calls), tool_calls=calls, per_response_sum_matches_cumulative=matches,
                rollout_ref=r['rollout_path'])


class CallFailure(Exception):
    pass


class ModelUnavailable(Exception):
    pass


async def call(role, revision, task_id, payload, sem):
    stage = ('initial_judge' if revision == 0 else 'fresh_final_judge') if role == 'judge' else ('initial_generation' if revision == 0 else 'one_revision_generation')
    folder = RUN/'tasks'/task_id.replace(':', '__')/f'round_{revision}'/role
    pack = dict(role=role, execution=(RUN/'prompts/execute.txt').read_text(), protocol=(RUN/'prompts'/(role+'.txt')).read_text(), data=payload)
    freeze(folder/'input.json', pack)
    identity = dict(task_id=task_id, stage=stage, revision=revision, model=MODEL, reasoning_effort=EFFORT,
                    input_sha256=digest(folder/'input.json'), prompt_sha256=digest(RUN/'prompts'/(role+'.txt')),
                    execution_sha256=digest(RUN/'prompts/execute.txt'), fork_turns='none')
    freeze(folder/'identity.json', identity)
    if (folder/'completed.json').exists():
        completed = read(folder/'completed.json')
        if completed['identity'] != identity:
            raise ValueError('cache_identity_mismatch')
        if completed['status'] != 'completed':
            raise CallFailure(completed['status'])
        return (folder/'response.txt').read_text()
    if (folder/'started.json').exists():
        raise CallFailure('prior_call_usage_or_completion_unknown_no_retry')
    message = '按以下完整输入包的execution和protocol处理单题单阶段。使用消息模式，只返回业务JSON，不调用工具，不读取文件，不启动子agent。\n'+json.dumps(pack, ensure_ascii=False, separators=(',', ':'))
    async with sem:
        freeze(folder/'started.json', dict(identity=identity, started_at=now()))
        cmd = ['codex', 'exec', '--skip-git-repo-check', '--ignore-user-config', '-m', MODEL,
               '-c', 'model_reasoning_effort="high"', '-s', 'read-only', '--json',
               '-o', str(folder/'response.txt'), '-']
        with (folder/'events.jsonl').open('xb') as out, (folder/'stderr.txt').open('xb') as err:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=out, stderr=err, cwd=str(ROOT))
            await proc.communicate(message.encode())
        thread_id, model_error = None, False
        for line in (folder/'events.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started':
                thread_id = event.get('thread_id')
            if event.get('type') in ('error', 'turn.failed'):
                error = json.dumps(event).lower()
                model_error = model_error or any(t in error for t in ('model_not_found', 'model is not supported', 'model does not exist', 'unsupported model', 'not available for'))
        usage = thread_usage(thread_id) if thread_id else dict(usage_unknown=True, error='launcher_did_not_create_thread')
        freeze(folder/'usage.json', {**identity, **usage})
        successful = proc.returncode == 0 and (folder/'response.txt').exists() and usage.get('actual_config_verified')
        status = 'completed' if successful else ('model_unavailable' if model_error else 'call_failed')
        freeze(folder/'completed.json', dict(identity=identity, status=status, returncode=proc.returncode, thread_id=thread_id, finished_at=now()))
        print(json.dumps(dict(task_id=task_id, stage=stage, status=status, tokens=(usage.get('actual_usage') or {}).get('total_tokens'))), flush=True)
        if model_error or (usage.get('model') and not usage.get('actual_config_verified')):
            raise ModelUnavailable('required_model_configuration_unavailable_or_mismatched')
        if not successful:
            raise CallFailure(status)
        return (folder/'response.txt').read_text()


async def process(task, sem):
    folder = RUN/'tasks'/task['task_id'].replace(':', '__')
    if (folder/'status.json').exists():
        return read(folder/'status.json')
    state = dict(task_id=task['task_id'], task_retained=True, rounds=[])
    if task['preparation_status'] != 'ready':
        state.update(status=task['preparation_status'], error=task.get('error'))
        freeze(folder/'status.json', state)
        return state
    original, view, audit = (read(task[n+'_ref']) for n in ('original', 'view', 'audit'))
    previous, feedback = None, None
    for revision in range(2):
        role = 'generate' if revision == 0 else 'revise'
        payload = dict(view) if revision == 0 else dict(view, previous_output=previous, revision_feedback=feedback)
        row = dict(revision=revision)
        state['rounds'].append(row)
        try:
            raw = await call(role, revision, task['task_id'], payload, sem)
        except CallFailure as exc:
            row.update(status='call_failed', error=str(exc))
            break
        try:
            previous = json.loads(raw)
        except ValueError:
            previous = dict(invalid_raw_content=raw)
            errors = [dict(path='/', actual=raw, expected='one complete JSON object; no Markdown, no truncation')]
        else:
            errors = validate_selection(previous, original, audit)
        freeze(folder/f'round_{revision}'/role/'output.json', previous)
        rubric = None
        if not errors:
            try:
                rubric = compile_rubric(previous, original, audit)
            except (ValueError, TypeError, KeyError) as exc:
                errors = [dict(path='/', actual=str(exc), expected='valid five-field compiled rubric with original citations')]
        freeze(folder/f'round_{revision}'/'validation.json', dict(errors=errors))
        if errors:
            row.update(status='invalid_generation', errors=errors)
            feedback = dict(kind='structure_errors', errors=errors,
                            legal_source_ids=[s['id'] for s in original['sources']],
                            legal_candidate_ids=[c['candidate_id'] for c in audit['candidates']],
                            legal_contract_ref_ids=[k for k in audit['reference_registry'] if k.startswith('K')])
            continue
        freeze(folder/f'round_{revision}'/'rubric.json', rubric)
        try:
            raw_judge = await call('judge', revision, task['task_id'], dict(view, candidate_rubric=rubric), sem)
        except CallFailure as exc:
            row.update(status='call_failed', error=str(exc), failed_stage='judge')
            break
        try:
            judgment = json.loads(raw_judge)
        except ValueError:
            judgment = dict(invalid_raw_content=raw_judge)
        freeze(folder/f'round_{revision}'/'judge/output.json', judgment)
        verdict = evaluate(judgment, task['task_id'], {r['id'] for r in rubric['rubric']}, {c['candidate_id'] for c in audit['candidates']})
        freeze(folder/f'round_{revision}'/'evaluation.json', verdict)
        row.update(verdict)
        if verdict['status'] == 'passed':
            freeze(RUN/'accepted'/(task['task_id'].replace(':', '__')+'.json'), rubric)
            state['accepted_revision'] = revision
            break
        if verdict['status'] == 'invalid_judge':
            break
        feedback = dict(kind='judge_feedback', judgment=judgment)
    state.update(status=state['rounds'][-1]['status'], first_status=state['rounds'][0]['status'],
                 entered_revision=len(state['rounds']) > 1)
    freeze(folder/'status.json', state)
    return state


async def run(limit=None, workers=3):
    m = manifest()
    verify_manifest(m)
    sem = asyncio.Semaphore(min(workers, m['concurrency']))
    tasks = m['tasks'][:limit] if limit else m['tasks']
    # The outer workers also bound resident source packages to three tasks.
    queue = asyncio.Queue()
    for task in tasks:
        queue.put_nowait(task)
    stop = asyncio.Event()

    async def worker():
        while not queue.empty() and not stop.is_set():
            task = queue.get_nowait()
            try:
                await process(task, sem)
            except ModelUnavailable as exc:
                stop.set()
                freeze(RUN/'model_unavailable.json', dict(error=str(exc), task_id=task['task_id'], at=now()))
            queue.task_done()
            snapshot(RUN/'progress.json', progress(m))

    await asyncio.gather(*(worker() for _ in range(min(workers, 3))))
    report()


def progress(m):
    states = [read(p) for p in (RUN/'tasks').glob('*/status.json')]
    return dict(frozen_tasks=len(m['tasks']), completed=len(states), remaining=len(m['tasks'])-len(states),
                statuses=dict(collections.Counter(s['status'] for s in states)), updated_at=now())


def report():
    m = manifest()
    states = [read(p) for p in (RUN/'tasks').glob('*/status.json')]
    usage = [read(p) for p in (RUN/'tasks').glob('*/round_*/*/usage.json')]
    # The startup availability check is real paid usage and is kept explicit.
    preflight = thread_usage('01a16df0-677f-71a0-b9f9-da81e5511c40')
    preflight.update(stage='model_preflight', task_id=None)
    usage.append(preflight)
    known, unknown, seen = [], [], set()
    for u in usage:
        tid = u.get('thread_id')
        if tid and tid in seen:
            raise ValueError('duplicate_thread_usage')
        if tid:
            seen.add(tid)
        (unknown if u.get('usage_unknown') else known).append(u)
    responses = {}
    for u in known:
        for rid, value in u.get('response_usage', {}).items():
            if rid in responses:
                raise ValueError('response_id_reused_between_threads')
            responses[rid] = value
    totals = {k:sum((u.get('actual_usage') or {}).get(k, 0) for u in known) for k in USAGE_KEYS}
    totals['noncached_input_tokens'] = totals['input_tokens']-totals['cached_input_tokens']
    stage_usage = {stage:dict(threads=sum(u.get('stage') == stage for u in usage),
                            **{k:sum((u.get('actual_usage') or {}).get(k, 0) for u in known if u.get('stage') == stage) for k in USAGE_KEYS}) for stage in STAGES+('model_preflight',)}
    first = sum(s.get('first_status') == 'passed' for s in states)
    passed = sum(s['status'] == 'passed' for s in states)
    revised = sum(s.get('entered_revision', False) for s in states)
    additional = sum(s['status'] == 'passed' and s.get('entered_revision', False) for s in states)
    completed = len(states) == len(m['tasks'])
    result = dict(completed=completed, tasks=states, denominator=len(m['tasks']), first_passed=first, final_passed=passed,
                  revision_tasks=revised, revision_additional_passed=additional,
                  failure_types=dict(collections.Counter(s['status'] for s in states if s['status'] != 'passed')),
                  totals=totals, stages=stage_usage, tokens_per_original=totals['total_tokens']/len(m['tasks']) if completed else None,
                  tokens_per_accepted=totals['total_tokens']/passed if completed and passed else None,
                  model_response_count=len(responses), tool_call_count=sum(u.get('tool_call_count', 0) for u in usage),
                  unknown_usage_calls=len(unknown), semantic_review_by_coordinator=False)
    snapshot(RUN/'usage.json', dict(totals=totals, stages=stage_usage, threads=usage, unknown_usage=unknown,
                                  unique_response_count=len(responses), cache_subset_of_input=True, reasoning_subset_of_output=True,
                                  scheduler_usage_excluded=True, read_at=now()))
    scheduler_id = os.environ.get('CODEX_THREAD_ID')
    if scheduler_id:
        snapshot(RUN/'scheduler_usage.json', dict(**thread_usage(scheduler_id),
                  accounting='Coordinator only, excluded from experiment totals; snapshot before final response', read_at=now()))
    snapshot(RUN/'results.json', result)
    snapshot(RUN/'failures.json', [s for s in states if s['status'] != 'passed'])
    snapshot(RUN/'progress.json', progress(m))
    baseline = read(ROOT/'runs/rubric_luna56_lean_10_20261024/usage.json')['totals']['total_tokens']
    lines = ['# Luna high 100题试跑', '',
             f'状态：{"已完成" if completed else "运行中或尚未完成"}；{len(states)}/{len(m["tasks"])}题已有最终状态。', '',
             f'首次通过 {first}/100，最终通过 {passed}/100；进入唯一一轮修订 {revised}题，修订新增通过 {additional}题。',
             '这是独立模型审查流程的通过率，不是人工准确率；主调度没有语义复核或手工改写rubric。', '',
             '| 实际用量 | tokens |', '|---|---:|']
    for k, v in totals.items():
        lines.append(f'| {k} | {v:,} |')
    lines += ['', f'模型响应 {len(responses)}次，工具调用 {result["tool_call_count"]}次；未知用量调用 {len(unknown)}个。',
              '缓存输入是输入子项，思考是输出子项，不重复计入总量。每线程只取最后累计usage一次，并与唯一response_id逐响应求和核对。',
              '全部生成、审查、修订、失败和模型可用性检查均计入；主调度用量另列，不计入子agent对照口径。未验证模型单价，不换算人民币。', '',
              '| 阶段 | 线程数 | 总token |', '|---|---:|---:|']
    for stage, u in stage_usage.items():
        lines.append(f'| {stage} | {u["threads"]} | {u["total_tokens"]:,} |')
    if completed:
        avg = result['tokens_per_original']
        accepted_avg = result['tokens_per_accepted']
        lines += ['', f'每道原题平均 {avg:,.2f} tokens；每份通过rubric平均 '+(f'{accepted_avg:,.2f}' if accepted_avg is not None else '未定义')+' tokens，包含所有失败成本。',
                  f'旧10题：每题 {baseline/10:,.2f}，每份通过版 {baseline/8:,.2f} tokens。',
                  f'本次每题相对变化 {(avg/(baseline/10)-1):+.2%}。'+(f'每份通过版相对变化 {(accepted_avg/(baseline/8)-1):+.2%}。' if accepted_avg else '')]
    lines += ['', '失败类型：'+json.dumps(result['failure_types'], ensure_ascii=False), '',
              '本轮按ID排序后使用固定seed=20261024无放回抽100题；原题、原始来源、词法候选、offset、所有版本、结构错误和独立审查结果均保留。',
              '与旧10题相比，样本、来源准备和prompt补充规则均改变。没有已验证的筛选器，故提供环境完整公开契约、工具清单及全部DDL，不沿用手工接口节选。',
              '压缩只移除JSON外层空白和已确认的schema示例元数据；真实example/examples业务字段保留；引文绑定原JSON字符串token的真实offset。',
              '每题每阶段为全新Codex会话，消息模式一次提交。审查只收到完整来源、候选和当前rubric；没有生成思考、旧版本、评分或反馈。',
              '完成100题后停止，不运行剩余9900题。', '']
    (RUN/'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps({k:result[k] for k in ('completed', 'denominator', 'first_passed', 'final_passed', 'revision_tasks', 'totals', 'unknown_usage_calls')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'report'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=3, choices=[1, 2, 3])
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'run':
        asyncio.run(run(args.limit, args.workers))
    else:
        report()
