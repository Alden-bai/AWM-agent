"""Windows/Linux continuation after the user's six-request recovery exception.

Default action is offline preflight. Paid continuation requires the explicit
run --continue-remaining flag. Reuses the frozen business state machine and
compiled stage caches; never runs a task with an existing terminal status.
"""
import argparse
import asyncio
import collections
import copy
import json
from pathlib import Path
import sys

import rubric_luna_batch03_recovery_v1 as recovery

ROOT, RUN, REPAIR = recovery.ROOT, recovery.RUN, recovery.REPAIR
CONT = REPAIR / 'continuation'
read, sha, local, durable = recovery.read, recovery.sha, recovery.local, recovery.durable


def load_business():
    deps = REPAIR / 'python_deps'
    if deps.exists():
        sys.path.insert(0, str(deps))
    import rubric_luna100 as business
    business.RUN = RUN
    business.read = lambda p: read(local(p))
    business.freeze = lambda p, value, raw=False: durable(p, value, raw=raw, immutable=True)
    return business


def preflight():
    contract = read(REPAIR / 'continuation_contract.json')
    auth = read(REPAIR / 'authorization.json')
    if not contract['six_stage_retries_complete']:
        raise ValueError('six_retries_not_complete')
    if sha(REPAIR / 'authorization.json') != contract['authorization_sha256']:
        raise ValueError('authorization_changed')
    if sha(RUN / 'manifest.json') != contract['original_manifest_sha256']:
        raise ValueError('manifest_changed')
    if sha(RUN / 'scheduler_state.json') != auth['evidence']['scheduler_sha256']:
        raise ValueError('legacy_scheduler_changed_reconcile_other_runner_first')
    for path, expected in auth['evidence']['implementation_hashes'].items():
        if sha(local(path)) != expected:
            raise ValueError('frozen_implementation_changed:' + path)
    for entry in contract['selections'].values():
        recovery.selected_result(ROOT / entry['selected_folder'], entry['identity'])
    manifest = read(RUN / 'manifest.json')
    for name, expected in manifest['source_file_hashes'].items():
        if sha(ROOT / 'data/awm-dde80a0' / (name + '.jsonl')) != expected:
            raise ValueError('dataset_changed')
    for task in manifest['tasks']:
        for name in ('original', 'view', 'audit'):
            if name + '_ref' in task and sha(local(task[name + '_ref'])) != task[name + '_sha256']:
                raise ValueError('task_input_changed:' + task['task_id'])
    business = load_business()
    terminal = {read(p)['task_id'] for p in RUN.glob('tasks/*/status.json')}
    if not terminal <= {t['task_id'] for t in manifest['tasks']}:
        raise ValueError('unexpected_terminal_task')
    return manifest, business, contract, dict(passed=True, paid_calls=0, frozen=len(manifest['tasks']),
        terminal=len(terminal), pending=len(manifest['tasks'])-len(terminal),
        old_holds_retained=6, new_concurrency=3, combined_concurrency_ceiling=9, hard_global_cap=12,
        original_business_code_reused=True, remaining_batch_started=False)


class Continuation:
    def __init__(self, manifest, business, contract):
        self.manifest, self.business, self.contract = manifest, business, contract
        self.tasks = {t['task_id']:t for t in manifest['tasks']}
        self.resolver = recovery.StageResolver()
        self.semaphore = asyncio.Semaphore(3)
        self.index_path = CONT / 'selected_attempts.json'
        self.index = read(self.index_path) if self.index_path.exists() else {}
        self.state_path = CONT / 'scheduler_state.json'
        self.state = read(self.state_path) if self.state_path.exists() else dict(
            old_remote_holds=contract['retained_old_remote_holds'], active={}, new_remote_holds={},
            stop_reason=None, new_concurrency_limit=3, peak_total_permits=6,
            dispatch_policy='fixed new concurrency 3 while six unresolved old permits remain; no automatic promotion')
        if self.state['old_remote_holds'] != contract['retained_old_remote_holds']:
            raise ValueError('old_holds_must_not_be_released')
        # A crash after a new completion can be reconciled, but never a bare
        # local process exit or an unfinished new request.
        for key, reservation in list(self.state['active'].items()):
            folder = ROOT / reservation['folder']
            recovery.selected_result(folder)
            identity = read(folder / 'identity.json')
            self.register_selection(identity, folder)
            self.state['active'].pop(key)
        if self.state['new_remote_holds'] or self.state.get('stop_reason') not in (None, 'window_handoff', 'all_tasks_terminal'):
            raise ValueError('new_failure_requires_reconciliation_not_covered_by_six_request_exception')
        self.state['stop_reason'] = None
        self.business.call = self.call

    def register_selection(self, identity, folder):
        role = 'judge' if 'judge' in identity['stage'] else ('generate' if identity['revision'] == 0 else 'revise')
        canonical = recovery.stage_folder(identity['task_id'], identity['revision'], role)
        self.index[canonical.relative_to(ROOT).as_posix()] = dict(
            selected_folder=folder.relative_to(ROOT).as_posix(), identity=identity,
            response_sha256=sha(folder / 'response.txt'), usage_sha256=sha(folder / 'usage.json'))
        durable(self.index_path, self.index)

    async def call(self, role, revision, task_id, payload, unused_sem):
        if task_id not in self.tasks or revision not in (0,1):
            raise ValueError('request_outside_frozen_batch')
        canonical = recovery.stage_folder(task_id, revision, role)
        key = canonical.relative_to(ROOT).as_posix()
        if key in self.index:
            entry = self.index[key]
            chosen = ROOT / entry['selected_folder']
            if sha(chosen / 'response.txt') != entry['response_sha256'] or sha(chosen / 'usage.json') != entry['usage_sha256']:
                raise ValueError('continuation_selected_artifact_changed')
            identity, pack = recovery.validate_input(chosen)
            if pack['data'] != payload:
                raise ValueError('continuation_cached_payload_changed')
            return recovery.selected_result(chosen, identity)
        if key in self.contract['selections'] or (canonical / 'completed.json').exists():
            return self.resolver.response(role, revision, task_id, payload)
        async with self.semaphore:
            recovery.assert_dispatch_allowed(self.state)
            stage = ('initial_judge' if revision == 0 else 'fresh_final_judge') if role == 'judge' else ('initial_generation' if revision == 0 else 'one_revision_generation')
            pack = dict(role=role, execution=(RUN / 'prompts/execute.txt').read_text(encoding='utf-8'),
                        protocol=(RUN / 'prompts' / (role + '.txt')).read_text(encoding='utf-8'), data=payload)
            import hashlib
            identity = dict(run_id=RUN.name, task_id=task_id, stage=stage, revision=revision,
                model=recovery.MODEL, reasoning_effort=recovery.EFFORT,
                input_sha256=hashlib.sha256(recovery.encode(pack).encode()).hexdigest(),
                prompt_sha256=sha(RUN / 'prompts' / (role + '.txt')),
                execution_sha256=sha(RUN / 'prompts/execute.txt'), fork_turns='none')
            # Fresh stages get an immutable canonical input; raw attempts stay
            # separate. Existing incomplete stages cannot silently be replaced.
            attempts_root = CONT / 'attempts'
            attempt = attempts_root / task_id.replace(':', '__') / stage / 'attempt_1'
            if (canonical / 'started.json').exists() and not (attempt / 'completed.json').exists():
                raise ValueError('unrecognized_original_request_unresolved')
            durable(canonical / 'input.json', pack, immutable=True)
            durable(canonical / 'identity.json', identity, immutable=True)
            request = dict(task_id=task_id, old_folder=key, old_thread_id=None,
                           identity=identity, key=recovery.key_for(identity))
            folder = await recovery.execute_one(request, self.state,
                state_path=self.state_path, attempts_root=attempts_root)
            self.register_selection(identity, folder)
            return recovery.selected_result(folder, identity)

    async def run(self):
        durable(self.state_path, self.state)
        pending = [t for t in self.manifest['tasks']
                   if not (RUN / 'tasks' / t['task_id'].replace(':','__') / 'status.json').exists()]
        queue = asyncio.Queue()
        for task in pending:
            queue.put_nowait(task)
        errors = []
        completed = 0
        async def worker():
            nonlocal completed
            while not queue.empty() and not self.state.get('stop_reason'):
                task = queue.get_nowait()
                try:
                    result = await self.business.process(task, None)
                    completed += 1
                    recovery.append(CONT / 'task_events.jsonl', dict(at=recovery.now(), task_id=task['task_id'], status=result['status']))
                    if completed % 25 == 0:
                        self.report()
                except Exception as exc:
                    self.state['stop_reason'] = self.state.get('stop_reason') or 'continuation_error_requires_review'
                    errors.append(dict(task_id=task['task_id'], error=repr(exc)))
                    durable(self.state_path, self.state)
                finally:
                    queue.task_done()
        # All workers drain healthy calls before the shared batch lock is freed.
        try:
            await asyncio.gather(*(worker() for _ in range(3)))
        finally:
            durable(CONT / 'errors.json', errors)
            self.report()
        if errors:
            raise RuntimeError('continuation_stopped_see_errors.json')
        self.state['stop_reason'] = 'all_tasks_terminal'
        durable(self.state_path, self.state)

    def report(self):
        states = {read(p)['task_id']:read(p) for p in RUN.glob('tasks/*/status.json')}
        usage_paths = list(RUN.glob('tasks/*/round_*/*/usage.json'))
        usage_paths += list((REPAIR / 'attempts').glob('*/*/attempt_1/usage.json'))
        usage_paths += list((CONT / 'attempts').glob('*/*/attempt_1/usage.json'))
        usages = [dict(read(p), artifact_ref=str(p)) for p in usage_paths]
        seen = set()
        for usage in usages:
            tid = usage.get('thread_id')
            if tid and tid in seen:
                raise ValueError('duplicate_thread_usage')
            if tid:
                seen.add(tid)
        for old in self.contract['old_unknown_usage_requests']:
            if old['thread_id'] not in seen:
                unknown = dict(old['identity'], thread_id=old['thread_id'], usage_unknown=True,
                               original_folder=old['original_folder'], server_completion_evidence=False)
                placeholder = REPAIR / 'old_unknown_usage' / (old['thread_id'] + '.json')
                durable(placeholder, unknown, immutable=True)
                usages.append(dict(unknown, artifact_ref=str(placeholder)))
        known = [u for u in usages if not u.get('usage_unknown') and u.get('actual_usage')]
        totals = {k:sum(u['actual_usage'].get(k,0) for u in known) for k in recovery.USAGE_KEYS}
        progress = dict(at=recovery.now(), frozen=len(self.tasks), terminal=len(states), pending=len(self.tasks)-len(states),
            statuses=dict(collections.Counter(s['status'] for s in states.values())), known_usage=totals,
            unknown_usage_requests=sum(bool(u.get('usage_unknown')) for u in usages),
            old_holds_retained=6, new_active=len(self.state['active']),
            new_remote_holds=len(self.state['new_remote_holds']), stop_reason=self.state.get('stop_reason'),
            final_bank_published=False, semantic_review_by_coordinator=False)
        durable(CONT / 'usage.json', dict(totals=totals, threads=usages, unknown_usage=[u for u in usages if u.get('usage_unknown')]))
        durable(CONT / 'progress.json', progress)
        self.project_registry(states, usages, progress)
        print(recovery.encode(progress), flush=True)
        return progress

    def project_registry(self, states, usages, progress):
        """Append authoritative events before replacing derived registry indices.

        A successfully recovered task may pass while its OLD attempt's usage
        remains unknown. Do not turn its result into usage_unknown solely because
        the retained accounting evidence contains an abandoned old attempt.
        """
        registry = ROOT / 'runs/rubric_luna_registry'
        with recovery.locked(registry / '.lock'):
            batches, records = {}, {}
            for line in (registry / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                for row in event.get('batches', []):
                    batches[row['run_id']] = row
                for row in event.get('tasks', []):
                    records[(row['dataset_id'], row['task_id'], row['run_id'])] = row
            if len(records) != 7000 or sum(k[2] == RUN.name for k in records) != 3000:
                raise ValueError('registry_cardinality_changed')
            changed = []
            for task_id, state in states.items():
                key = (self.manifest['dataset_id'], task_id, RUN.name)
                previous = records[key]
                row = copy.deepcopy(previous)
                folder = RUN / 'tasks' / task_id.replace(':', '__')
                rounds = state['rounds']
                version = state.get('accepted_revision', len(rounds)-1)
                task_usages = [u for u in usages if u.get('task_id') == task_id]
                refs = sorted({u['artifact_ref'] for u in task_usages if u.get('artifact_ref')})
                unknown_refs = sorted({u['artifact_ref'] for u in task_usages if u.get('usage_unknown') and u.get('artifact_ref')})
                row.update(status=state['status'], state_ref=str(folder / 'status.json'),
                    final_version=version, revision_count=max(0, len(rounds)-1),
                    model_call_occurred=bool(task_usages), actual_usage_refs=refs, unknown_usage_refs=unknown_refs,
                    first_judge_ref=str(folder / 'round_0/judge/output.json') if (folder / 'round_0/judge/output.json').exists() else None,
                    final_judge_ref=str(folder / 'round_1/judge/output.json') if (folder / 'round_1/judge/output.json').exists() else None,
                    final_rubric_ref=str(folder / f'round_{version}/rubric.json') if (folder / f'round_{version}/rubric.json').exists() else None,
                    accepted_ref=str(RUN / 'accepted' / (task_id.replace(':','__') + '.json')) if state['status']=='passed' else None,
                    errors=[dict(revision=r.get('revision'), status=r.get('status'), error=r.get('error'),
                                 validation_ref=str(folder / f"round_{r.get('revision')}/validation.json"))
                            for r in rounds if r.get('status') != 'passed'])
                selected = []
                for p in folder.glob('round_*/*/identity.json'):
                    canonical = p.parent.relative_to(ROOT).as_posix()
                    entry = self.index.get(canonical) or self.contract['selections'].get(canonical)
                    chosen = ROOT / entry['selected_folder'] if entry else p.parent
                    if (chosen / 'completed.json').exists() and read(chosen / 'completed.json')['status'] == 'completed':
                        selected.append(read(p)['stage'])
                if selected:
                    row['last_completed_stage'] = max(selected, key=self.business.STAGES.index)
                if row != previous:
                    row['updated_at'] = recovery.now()
                    changed.append(row)
                    records[key] = row
            batch = copy.deepcopy(batches[RUN.name])
            batch.update(status='completed_pending_publication' if progress['pending']==0 else 'partial_recovered',
                terminal=progress['terminal'], pending=progress['pending'], final_passed=progress['statuses'].get('passed',0),
                luna_actual_usage=progress['known_usage'], unknown_usage=[u for u in usages if u.get('usage_unknown')],
                usage_ref=str(CONT / 'usage.json'), recovery_contract_ref=str(REPAIR / 'continuation_contract.json'),
                progress_ref=str(CONT / 'progress.json'), old_remote_holds_retained=6)
            changed_batches = [batch] if batch != batches[RUN.name] else []
            if changed or changed_batches:
                recovery.append(registry / 'events.jsonl', dict(at=recovery.now(),
                    reason='授权重试后的续跑登记；旧未知用量与新结果分开，保留全部7000题',
                    batches=changed_batches, tasks=changed))
            batches[RUN.name] = batch
            durable(registry / 'batches.json', batches)
            durable(registry / 'tasks.jsonl', ''.join(recovery.encode(v)+'\n' for _,v in sorted(records.items())), raw=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'run'])
    parser.add_argument('--continue-remaining', action='store_true')
    args = parser.parse_args()
    if args.action == 'run' and not args.continue_remaining:
        parser.error('run requires --continue-remaining after the user requests continuation')
    if args.action == 'run':
        acceptance = read(REPAIR / 'offline_acceptance.json')
        if not acceptance['passed'] or acceptance['paid_calls'] != 0:
            raise ValueError('passing_offline_acceptance_required')
        for path, expected in acceptance['implementation_hashes'].items():
            if sha(ROOT / path) != expected:
                raise ValueError('continuation_code_changed_retest_and_record_new_acceptance:' + path)
    with recovery.locked(RUN / '.runner.lock'):
        manifest, business, contract, evidence = preflight()
        if args.action == 'preflight':
            durable(REPAIR / 'continuation_preflight.json', evidence)
            print(recovery.encode(evidence))
            return
        runner = Continuation(manifest, business, contract)
        asyncio.run(runner.run())


if __name__ == '__main__':
    main()
