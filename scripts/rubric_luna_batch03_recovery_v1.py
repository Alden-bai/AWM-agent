"""Explicitly authorized, append-only repair of six batch03 stage requests.

This entry never dispatches the remaining batch. The original stage directories,
manifests, scheduler state, and unknown usage remain evidence, not mutable caches.
StageResolver and continuation_contract.json provide the selected attempt mapping
for a subsequent continuation runner. Semantic work is exclusively Luna/high.
"""
import argparse
import asyncio
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/rubric_luna_high_3000_batch03'
REPAIR = RUN / 'recovery_20261102'
MODEL, EFFORT = 'gpt-5.6-luna', 'high'
SIX = (
    ('crm_applicant_tracking_1:1', 0, 'generate'),
    ('billing_and_payments_1:1', 0, 'judge'),
    ('booking_and_scheduling_16:7', 1, 'judge'),
    ('form_workflow_and_submission_1:1', 0, 'judge'),
    ('job_board_9:3', 0, 'generate'),
    ('professional_networking_11:6', 1, 'judge'),
)
PREFIX = '按以下完整输入包的execution和protocol处理单题单阶段。使用消息模式，只返回业务JSON，不调用工具，不读取文件，不启动子agent。\n'
USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local(path):
    value = str(path).replace('\\', '/')
    prefix = '/home/bjx/awm-agent/'
    return ROOT / value[len(prefix):] if value.startswith(prefix) else Path(path)


def durable(path, value, *, raw=False, immutable=False):
    path = Path(path)
    content = value if raw else encode(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_text(encoding='utf-8') != content:
            raise ValueError('immutable_record_conflict:' + str(path))
        return path
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != 'nt':
            fd_dir = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd_dir)
            finally:
                os.close(fd_dir)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='') as stream:
        stream.write(encode(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def locked(path):
    """Use the SAME batch lock inode/path; never delete or replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def stage_folder(task_id, revision, role):
    return RUN / 'tasks' / task_id.replace(':', '__') / f'round_{revision}' / role


def key_for(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def event_rows(folder):
    path = folder / 'events.jsonl'
    result = []
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                continue
    return result


def validate_input(folder):
    identity, pack = read(folder / 'identity.json'), read(folder / 'input.json')
    if hashlib.sha256(encode(pack).encode()).hexdigest() != identity['input_sha256']:
        raise ValueError('original_message_hash_mismatch:' + str(folder))
    if identity['model'] != MODEL or identity['reasoning_effort'] != EFFORT:
        raise ValueError('original_model_mismatch')
    for field, name in [('prompt_sha256', pack['role'] + '.txt'), ('execution_sha256', 'execute.txt')]:
        if sha(RUN / 'prompts' / name) != identity[field]:
            raise ValueError('frozen_prompt_hash_mismatch:' + name)
    if pack['execution'] != (RUN / 'prompts/execute.txt').read_text(encoding='utf-8'):
        raise ValueError('execution_content_mismatch')
    if pack['protocol'] != (RUN / 'prompts' / (pack['role'] + '.txt')).read_text(encoding='utf-8'):
        raise ValueError('protocol_content_mismatch')
    return identity, pack


def preflight():
    manifest = read(RUN / 'manifest.json')
    state = read(RUN / 'scheduler_state.json')
    holds = {**state.get('active', {}), **state.get('remote_holds', {})}
    if len(holds) != 6 or state['stop_reason'] != 'unresolved_remote_request':
        raise ValueError('old_scheduler_changed_reassess_before_retry')
    frozen_ids = {t['task_id'] for t in manifest['tasks']}
    if len(frozen_ids) != manifest['sample_size'] or manifest['sample_size'] != 3000:
        raise ValueError('manifest_identity_mismatch')
    rows = []
    for task_id, revision, role in SIX:
        folder = stage_folder(task_id, revision, role)
        identity, pack = validate_input(folder)
        key = key_for(identity)
        if key not in holds or task_id not in frozen_ids:
            raise ValueError('authorized_request_does_not_match_hold')
        if (folder.parent.parent / 'status.json').exists():
            raise ValueError('do_not_retry_terminal_task:' + task_id)
        events = event_rows(folder)
        if any(e['type'] == 'turn.completed' for e in events):
            raise ValueError('new_completion_evidence_found_reconcile_first')
        thread_id = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
        rows.append(dict(task_id=task_id, revision=revision, role=role, key=key,
                         old_thread_id=thread_id, old_folder=str(folder.relative_to(ROOT)),
                         identity=identity, evidence_hashes={str(p.relative_to(ROOT)): sha(p)
                                                           for p in folder.iterdir() if p.is_file()}))
    if {r['key'] for r in rows} != set(holds):
        raise ValueError('unaccounted_old_hold')
    dependencies = read(RUN / 'execution_manifest_v3.json')['implementation_hashes']
    for path, expected in dependencies.items():
        if sha(local(path)) != expected:
            raise ValueError('frozen_implementation_changed:' + path)
    for name, expected in manifest['source_file_hashes'].items():
        if sha(ROOT / 'data/awm-dde80a0' / (name + '.jsonl')) != expected:
            raise ValueError('source_changed:' + name)
    return dict(manifest_sha256=sha(RUN / 'manifest.json'), scheduler_sha256=sha(RUN / 'scheduler_state.json'),
                old_holds=holds, requests=rows, implementation_hashes=dependencies,
                terminal_hashes={str(p.relative_to(ROOT)): sha(p) for p in RUN.glob('tasks/*/status.json')})


def authorize():
    evidence = preflight()
    path = REPAIR / 'authorization.json'
    if path.exists():
        prior = read(path)
        if prior['evidence'] != evidence:
            raise ValueError('authorization_snapshot_changed')
        return prior
    authorization = dict(created_at=now(), source='explicit_user_message_in_current_chat',
        instruction='允许对这6个状态不明的请求重新执行，接受旧请求可能已经产生用量、重试可能造成额外消耗；保留旧请求、原始记录和未知用量，不把旧请求伪造为已结束；旧请求可能在途占用仍计入全局并发上限。',
        scope='exactly_one_new_attempt_for_each_of_six_existing_stage_requests; stop_before_remaining_batch',
        old_requests_ended=False, old_unknown_usage_preserved=True, hard_global_limit=12,
        retained_old_permits=6, new_retry_concurrency=1, automatic_retry_of_new_attempts=False,
        evidence=evidence)
    durable(path, authorization, immutable=True)
    return authorization


def thread_usage(thread_id):
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    db_path = codex_home / 'state_5.sqlite'
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT id, model, reasoning_effort, rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
    if row is None:
        return dict(thread_id=thread_id, usage_unknown=True, error='local_thread_metadata_missing')
    rollout = Path(row['rollout_path'])
    contexts, responses, cumulative, tools = [], {}, None, []
    for line in rollout.read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        payload = event.get('payload', {})
        if event['type'] == 'turn_context':
            contexts.append((payload.get('model'), payload.get('effort')))
        if event['type'] == 'token_usage_record':
            rid = payload['response_id']
            if rid in responses and responses[rid] != payload['usage']:
                raise ValueError('response_usage_conflict')
            responses[rid] = payload['usage']
        if event['type'] == 'event_msg' and payload.get('type') == 'token_count' and payload.get('info'):
            cumulative = payload['info'].get('total_token_usage') or cumulative
        if event['type'] == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call', 'web_search_call'):
            tools.append(dict(type=payload['type'], name=payload.get('name')))
    totals = {k: sum(u.get(k, 0) for u in responses.values()) for k in USAGE_KEYS}
    actual = cumulative or (totals if responses else None)
    match = bool(cumulative and responses) and all(totals[k] == cumulative.get(k, 0) for k in USAGE_KEYS)
    if cumulative and responses and not match:
        raise ValueError('usage_cumulative_response_mismatch')
    verified = row['model'] == MODEL and row['reasoning_effort'] == EFFORT and bool(contexts) and all(c == (MODEL, EFFORT) for c in contexts)
    return dict(thread_id=thread_id, model=row['model'], reasoning_effort=row['reasoning_effort'],
                actual_config_verified=verified, actual_usage=actual, response_usage=responses,
                usage_unknown=actual is None, model_response_count=len(responses), tool_call_count=len(tools),
                tool_calls=tools, per_response_sum_matches_cumulative=match, rollout_ref=str(rollout),
                rollout_sha256=sha(rollout), config_contexts=contexts)


def selected_result(folder, expected_identity=None):
    identity = read(folder / 'identity.json')
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('selected_attempt_identity_mismatch')
    completed, usage = read(folder / 'completed.json'), read(folder / 'usage.json')
    if completed['status'] != 'completed' or not completed.get('server_completion_evidence'):
        raise ValueError('selected_attempt_not_completed')
    if completed['identity'] != identity or sha(folder / 'response.txt') != completed['response_sha256']:
        raise ValueError('selected_attempt_integrity_failed')
    if not usage.get('actual_config_verified') or usage.get('usage_unknown') or usage.get('tool_call_count'):
        raise ValueError('selected_attempt_configuration_or_usage_invalid')
    if usage['model'] != MODEL or usage['reasoning_effort'] != EFFORT:
        raise ValueError('selected_attempt_wrong_model')
    return (folder / 'response.txt').read_text(encoding='utf-8')


def permit_count(state):
    return len(state['old_remote_holds']) + len(state['active']) + len(state['new_remote_holds'])


def assert_dispatch_allowed(state):
    if state.get('stop_reason') or len(state['active']) >= state.get('new_concurrency_limit', 1) or state['new_remote_holds']:
        raise ValueError('new_attempt_unresolved_no_automatic_retry')
    if permit_count(state) + 1 > 12:
        raise ValueError('global_concurrency_cap')


async def execute_one(request, state, *, state_path=None, attempts_root=None):
    state_path = state_path or REPAIR / 'state.json'
    original = ROOT / request['old_folder']
    identity, pack = validate_input(original)
    folder = (attempts_root or REPAIR / 'attempts') / request['task_id'].replace(':', '__') / identity['stage'] / 'attempt_1'
    if (folder / 'completed.json').exists():
        selected_result(folder, identity)
        # Crash after completed.json but before state commit: release ONLY this
        # new attempt, with positive completion evidence. Old holds never move.
        state['active'].pop(request['key'], None)
        state['new_remote_holds'].pop(request['key'], None)
        durable(state_path, state)
        return folder
    if (folder / 'launch_intent.json').exists():
        raise ValueError('attempt_already_launched_reconcile_without_resubmission:' + str(folder))
    assert_dispatch_allowed(state)
    durable(folder / 'identity.json', identity, immutable=True)
    durable(folder / 'input.json', pack, immutable=True)
    durable(folder / 'attempt.json', dict(attempt_number=1, retry_of=request['old_thread_id'],
        kind='authorized_retry' if request['old_thread_id'] else 'new_stage',
        old_request_key=request['key'], original_folder=request['old_folder'],
        authorization_sha256=sha(REPAIR / 'authorization.json'), original_request_ended=False), immutable=True)
    state['active'][request['key']] = dict(folder=str(folder.relative_to(ROOT)), phase='intent', at=now())
    state['peak_total_permits'] = max(state['peak_total_permits'], permit_count(state))
    durable(state_path, state)
    durable(folder / 'launch_intent.json', dict(at=now(), identity=identity, total_permits=permit_count(state)), immutable=True)
    append(REPAIR / 'events.jsonl', dict(at=now(), event='authorized_retry_launch', key=request['key'], total_permits=permit_count(state)))
    cmd = [shutil.which('codex') or 'codex', 'exec', '--skip-git-repo-check', '--ignore-user-config',
           '-m', MODEL, '-c', 'model_reasoning_effort="high"', '-s', 'read-only', '--json',
           '-o', str(folder / 'response.pending.txt'), '-']
    print(encode(dict(event='retry_start', task_id=request['task_id'], stage=identity['stage'], total_permits=permit_count(state))), flush=True)
    started = time.monotonic()
    with (folder / 'events.jsonl').open('xb') as out, (folder / 'stderr.txt').open('xb') as err:
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=out, stderr=err,
            cwd=str(ROOT), **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}))
        durable(folder / 'started.json', dict(at=now(), pid=proc.pid, platform=sys.platform, identity=identity), immutable=True)
        state['active'][request['key']].update(pid=proc.pid, phase='spawned')
        durable(state_path, state)
        try:
            await proc.communicate((PREFIX + encode(pack)).encode())
        except BaseException:
            # Keep the permit as remotely unknown even after local tree cleanup.
            if proc.returncode is None:
                if os.name == 'nt':
                    cleanup = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await asyncio.shield(cleanup.wait())
                else:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.shield(proc.wait())
            state['new_remote_holds'][request['key']] = state['active'].pop(request['key'])
            state['stop_reason'] = 'interrupted_request_requires_reconciliation'
            durable(state_path, state)
            raise
    events = event_rows(folder)
    tid = next((e['thread_id'] for e in events if e['type'] == 'thread.started'), None)
    done = any(e['type'] == 'turn.completed' for e in events)
    usage = thread_usage(tid) if tid else dict(usage_unknown=True, error='no_thread_started')
    durable(folder / 'usage.json', {**identity, **usage}, immutable=True)
    if usage.get('rollout_ref'):
        durable(folder / 'rollout_snapshot.jsonl', Path(usage['rollout_ref']).read_text(encoding='utf-8'), raw=True, immutable=True)
    pending = folder / 'response.pending.txt'
    if pending.exists():
        durable(folder / 'response.txt', pending.read_text(encoding='utf-8'), raw=True, immutable=True)
    success = proc.returncode == 0 and done and pending.exists() and usage.get('actual_config_verified') and not usage.get('usage_unknown') and not usage.get('tool_call_count')
    durable(folder / 'completed.json', dict(identity=identity, status='completed' if success else 'call_failed',
        thread_id=tid, returncode=proc.returncode, server_completion_evidence=done, finished_at=now(),
        response_sha256=sha(folder / 'response.txt') if pending.exists() else None), immutable=True)
    active = state['active'].pop(request['key'])
    if not done:
        state['new_remote_holds'][request['key']] = active
    if not success:
        state['stop_reason'] = 'retry_failed_requires_review'
    durable(state_path, state)
    append(REPAIR / 'events.jsonl', dict(at=now(), event='retry_finished', task_id=request['task_id'],
        stage=identity['stage'], success=bool(success), seconds=time.monotonic()-started,
        total_permits=permit_count(state), old_permits_released=0))
    print(encode(dict(event='retry_finished', task_id=request['task_id'], success=bool(success),
                     tokens=(usage.get('actual_usage') or {}).get('total_tokens'))), flush=True)
    selected_result(folder, identity)
    return folder


def build_contract(authorization):
    selections, usage_rows = {}, []
    for request in authorization['evidence']['requests']:
        folder = REPAIR / 'attempts' / request['task_id'].replace(':', '__') / request['identity']['stage'] / 'attempt_1'
        if not (folder / 'completed.json').exists():
            continue
        selected_result(folder, request['identity'])
        usage = read(folder / 'usage.json')
        usage_rows.append(usage)
        selections[request['old_folder'].replace('\\', '/')] = dict(
            selected_folder=str(folder.relative_to(ROOT)).replace('\\', '/'),
            identity=request['identity'], old_thread_id=request['old_thread_id'],
            new_thread_id=usage['thread_id'], response_sha256=sha(folder / 'response.txt'),
            usage_sha256=sha(folder / 'usage.json'), old_request_ended=False)
    if len({u['thread_id'] for u in usage_rows}) != len(usage_rows):
        raise ValueError('duplicate_retry_thread')
    responses = [rid for u in usage_rows for rid in u.get('response_usage', {})]
    if len(set(responses)) != len(responses):
        raise ValueError('duplicate_retry_response')
    for path, expected in authorization['evidence']['terminal_hashes'].items():
        if sha(ROOT / path) != expected:
            raise ValueError('existing_terminal_changed')
    for request in authorization['evidence']['requests']:
        for path, expected in request['evidence_hashes'].items():
            if sha(ROOT / path) != expected:
                raise ValueError('old_request_evidence_changed')
    if sha(RUN / 'scheduler_state.json') != authorization['evidence']['scheduler_sha256']:
        raise ValueError('old_scheduler_evidence_changed')
    contract = dict(at=now(), version='batch03-authorized-recovery-v1',
        original_manifest_sha256=authorization['evidence']['manifest_sha256'],
        authorization_ref=str((REPAIR / 'authorization.json').relative_to(ROOT)),
        authorization_sha256=sha(REPAIR / 'authorization.json'), selections=selections,
        six_stage_retries_complete=len(selections)==6, remaining_batch_started=False,
        retained_old_remote_holds=authorization['evidence']['old_holds'],
        old_unknown_usage_requests=[dict(thread_id=r['old_thread_id'], identity=r['identity'], usage_unknown=True,
                                   original_folder=r['old_folder']) for r in authorization['evidence']['requests']],
        hard_global_concurrency=12, maximum_new_inflight_while_six_old_holds_remain=6,
        recommended_new_initial_concurrency=3, allow_automatic_retry_of_any_other_unknown_request=False,
        retry_known_usage={k:sum(u['actual_usage'].get(k,0) for u in usage_rows) for k in USAGE_KEYS},
        recovery_entry=str(Path(__file__).relative_to(ROOT)), recovery_entry_sha256=sha(__file__),
        integration='Use StageResolver for stage caches; retain old holds in runtime capacity; never clear old scheduler state to bypass the old runner guard.')
    durable(REPAIR / 'continuation_contract.json', contract)
    return contract


class StageResolver:
    """Shared, read-only stage selection for offline replay / future continuation."""
    def __init__(self, contract_path=None):
        self.contract_path = contract_path or REPAIR / 'continuation_contract.json'
        self.contract = read(self.contract_path)
        if self.contract['original_manifest_sha256'] != sha(RUN / 'manifest.json'):
            raise ValueError('continuation_manifest_changed')

    def folder(self, canonical):
        key = Path(canonical).relative_to(ROOT).as_posix()
        selection = self.contract['selections'].get(key)
        if selection:
            chosen = ROOT / selection['selected_folder']
            selected_result(chosen, selection['identity'])
            if sha(chosen / 'response.txt') != selection['response_sha256']:
                raise ValueError('selected_response_changed')
            if sha(chosen / 'usage.json') != selection['usage_sha256']:
                raise ValueError('selected_usage_changed')
            return chosen
        return Path(canonical)

    def response(self, role, revision, task_id, payload=None):
        canonical = stage_folder(task_id, revision, role)
        folder = self.folder(canonical)
        if not (folder / 'identity.json').exists():
            return None
        identity, pack = validate_input(folder)
        if payload is not None and pack['data'] != payload:
            raise ValueError('replayed_stage_payload_changed:' + str(canonical))
        return selected_result(folder, identity)


def verify_recovery():
    """No paid calls and no canonical task writes: validate only recovered stages.

    The same frozen compiler/evaluator will be called again by base.process on
    continuation. A rejected judge is a valid stage result, not a retry failure.
    """
    deps = REPAIR / 'python_deps'
    if deps.exists():
        sys.path.insert(0, str(deps))
    from rubric_candidates_100 import validate_selection, compile_rubric, evaluate
    authorization = read(REPAIR / 'authorization.json')
    contract = build_contract(authorization)
    if not contract['six_stage_retries_complete']:
        raise ValueError('not_all_six_retries_completed')
    tasks = {t['task_id']: t for t in read(RUN / 'manifest.json')['tasks']}
    resolver = StageResolver()
    validations = []
    for task_id, revision, role in SIX:
        task = tasks[task_id]
        canonical = stage_folder(task_id, revision, role)
        folder = resolver.folder(canonical)
        raw = resolver.response(role, revision, task_id)
        original, audit = read(local(task['original_ref'])), read(local(task['audit_ref']))
        try:
            output = json.loads(raw)
        except ValueError:
            output = {'invalid_raw_content': raw}
        if role == 'judge':
            rubric = read(canonical.parent / 'rubric.json')
            verdict = evaluate(output, task_id, {r['id'] for r in rubric['rubric']},
                               {c['candidate_id'] for c in audit['candidates']})
            validation = dict(task_id=task_id, revision=revision, role=role, verdict=verdict,
                next_action=('commit_terminal_on_replay' if revision == 1 or verdict['status'] in ('passed', 'invalid_judge')
                             else 'one_revision_generation'))
        else:
            errors = validate_selection(output, original, audit)
            compiled = None
            if not errors:
                try:
                    compiled = compile_rubric(output, original, audit)
                except (ValueError, TypeError, KeyError) as exc:
                    errors = [dict(path='/', actual=str(exc), expected='valid compiled rubric')]
            validation = dict(task_id=task_id, revision=revision, role=role, errors=errors,
                rubric_compiled=compiled is not None, next_action='initial_judge' if not errors else 'one_revision_generation')
        durable(folder / 'offline_validation.json', validation)
        validations.append(validation)
    # Every original request is accounted for: select 310 original completed
    # requests + six independently completed replacements, never either twice.
    stage_cache = []
    for identity_path in RUN.glob('tasks/*/round_*/*/identity.json'):
        canonical = identity_path.parent
        folder = resolver.folder(canonical)
        identity = read(identity_path)
        selected_result(folder, identity)
        usage = read(folder / 'usage.json')
        stage_cache.append(dict(task_id=identity['task_id'], stage=identity['stage'],
            canonical_folder=canonical.relative_to(ROOT).as_posix(),
            selected_folder=folder.relative_to(ROOT).as_posix(), input_sha256=identity['input_sha256'],
            response_sha256=sha(folder / 'response.txt'), usage_sha256=sha(folder / 'usage.json'),
            thread_id=usage['thread_id'], replacement=folder != canonical))
    terminal = {read(p)['task_id'] for p in RUN.glob('tasks/*/status.json')}
    queue = [t['task_id'] for t in tasks.values() if t['task_id'] not in terminal]
    if len(stage_cache) != 316 or sum(x['replacement'] for x in stage_cache) != 6:
        raise ValueError('stage_cache_cardinality_changed')
    if len({x['thread_id'] for x in stage_cache}) != len(stage_cache):
        raise ValueError('selected_stage_thread_duplicate')
    durable(REPAIR / 'stage_cache_index.json', stage_cache)
    durable(REPAIR / 'continuation_queue.json', dict(manifest_sha256=sha(RUN / 'manifest.json'),
        terminal_ids=sorted(terminal), pending_ids=queue, pending_count=len(queue), resampled=False))
    report = dict(at=now(), passed=True, paid_calls=0, model_stage_replacements=6,
        selected_completed_stages=len(stage_cache), original_completed_stages=310,
        terminal=len(terminal), pending=len(queue), old_remote_holds_retained=6,
        old_unknown_usage_preserved=6, stage_validations=validations,
        remaining_batch_started=False, original_pipeline_files_unchanged=True)
    durable(REPAIR / 'verification.json', report)
    print(encode({k:v for k,v in report.items() if k != 'stage_validations'}), flush=True)
    return report


async def retry_six():
    with locked(RUN / '.runner.lock'):
        authorization = authorize()
        state_path = REPAIR / 'state.json'
        state = read(state_path) if state_path.exists() else dict(old_remote_holds=authorization['evidence']['old_holds'],
            active={}, new_remote_holds={}, peak_total_permits=6, stop_reason=None)
        durable(state_path, state)
        for request in authorization['evidence']['requests']:
            await execute_one(request, state)
            build_contract(authorization)
        contract = build_contract(authorization)
        state['stage'] = 'six_requests_recovered_waiting_for_user_continuation'
        durable(state_path, state)
        print(encode(dict(six_retries_complete=contract['six_stage_retries_complete'],
            retry_tokens=contract['retry_known_usage']['total_tokens'], old_holds_retained=6,
            remaining_batch_started=False)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'retry-six', 'verify-recovery', 'status'])
    args = parser.parse_args()
    if args.action == 'retry-six':
        asyncio.run(retry_six())
    elif args.action == 'verify-recovery':
        with locked(RUN / '.runner.lock'):
            verify_recovery()
    elif args.action == 'preflight':
        evidence = preflight()
        print(encode(dict(passed=True, paid_calls=0, preserved_old_holds=len(evidence['old_holds']),
                         existing_terminal=len(evidence['terminal_hashes']), six_inputs_verified=True)))
    else:
        contract = read(REPAIR / 'continuation_contract.json')
        print(encode({k:contract[k] for k in ('six_stage_retries_complete', 'remaining_batch_started', 'retry_known_usage')}))


if __name__ == '__main__':
    main()
