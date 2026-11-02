"""Finish only previously started batch03 pilot tasks, preserving frozen inputs."""
import argparse
import asyncio
import copy
import json
from pathlib import Path

import rubric_luna_batch03 as batch


def select_pending(manifest, directory):
    selected = []
    for task in manifest['tasks']:
        folder = directory/'tasks'/task['task_id'].replace(':', '__')
        if list(folder.glob('round_*/*/started.json')) and not (folder/'status.json').exists():
            selected.append(task)
    if not 1 <= len(selected) <= 13:
        raise ValueError('pilot_requires_1_to_13_previously_started_unfinished_tasks')
    return selected


async def execute(manifest, selected):
    scoped = dict(manifest, tasks=selected)
    trial = batch.Trial(scoped, seconds=180, max_calls=13)

    async def stop_when_finished():
        while len(trial.results) < len(selected):
            await asyncio.sleep(.1)
        await trial.gate.stop('pilot_scope_complete')

    watcher = asyncio.create_task(stop_when_finished())
    try:
        return await trial.run()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-id', required=True)
    args = parser.parse_args()
    if not args.test_id.replace('_', '').replace('-', '').isalnum():
        parser.error('invalid test-id')
    batch.configure()
    with batch.legacy.locked(batch.RUN/'.runner.lock', blocking=False):
        manifest, chain = batch.legacy.execution_manifest(batch.RUN)
        batch.legacy.verify(manifest)
        batch.orphan_check()
        prior = batch.read(batch.RUN/'scheduler_state.json')
        if prior.get('active') or prior.get('remote_holds'):
            raise RuntimeError('unresolved_requests_require_reconciliation')
        if prior.get('stop_reason') not in (None, 'trial_call_limit', 'trial_dispatch_deadline',
                                          'user_stop', 'pilot_scope_complete'):
            raise RuntimeError('prior_failure_requires_reconciliation')
        selected = select_pending(manifest, batch.RUN)
        directory = batch.RUN/'pilot_tests'/args.test_id
        if directory.exists():
            raise RuntimeError('pilot_already_prepared_no_automatic_repeat')
        before = {str(p): batch.digest(p) for pattern in
                  ('tasks/*/round_*/*/response.txt', 'tasks/*/round_*/*/usage.json',
                   'tasks/*/round_*/*/completed.json') for p in batch.RUN.glob(pattern)}
        batch.freeze(directory/'baseline.json', dict(at=batch.now(), artifact_hashes=before,
            totals=batch.read(batch.RUN/'usage.json')['totals'],
            prior_summary=batch.read(batch.RUN/'trial_summary.json'),
            original_manifest_sha256=batch.digest(batch.RUN/'manifest.json')))
        execution = copy.deepcopy(manifest)
        execution.update(original_frozen_manifest_ref=str(batch.RUN/'manifest.json'),
            original_frozen_manifest_sha256=batch.digest(batch.RUN/'manifest.json'),
            previous_execution_manifest_ref=chain[-1]['path'], created_for_execution_at=batch.now())
        execution.pop('diagnostic_override', None)
        execution['bounded_pilot'] = dict(task_ids=[t['task_id'] for t in selected],
            initial_concurrency=3, max_new_calls=13, dispatch_seconds=180,
            purpose='user requested pre-3000 quality and safety pilot', record_ref=str(directory))
        execution['implementation_hashes'][str(Path(__file__).resolve())] = batch.digest(__file__)
        execution_path = batch.RUN/f'execution_manifest_v{len(chain)}.json'
        batch.freeze(execution_path, execution)
        batch.legacy.verify(batch.legacy.execution_manifest(batch.RUN)[0])
        with batch.legacy.locked(batch.REGISTRY/'.lock'):
            batches, _ = batch.legacy.replay()
            record = batch.legacy.batch_record(execution, batches[batch.RUN_ID])
            record.pop('diagnostic_override', None)
            record['bounded_pilot'] = execution['bounded_pilot']
            batch.legacy.commit('用户要求3000题前试跑；仅补齐已开始题目的完整质量链路', [record])
        batch.freeze(directory/'authorization.json', execution['bounded_pilot'])
        batch.freeze(directory/'started.json', dict(at=batch.now()))
        summary = asyncio.run(execute(execution, selected))
        batch.freeze(directory/'summary.json', summary)
        batch.report(execution)
        assert all(batch.digest(p) == sha for p, sha in before.items())
        after = batch.read(batch.RUN/'usage.json')
        batch.freeze(directory/'after_usage.json', after)
        batch.freeze(directory/'verification.json', dict(
            previous_raw_artifacts_unchanged=True,
            selected_task_ids=[t['task_id'] for t in selected],
            remaining=[t['task_id'] for t in selected if not
                       (batch.RUN/'tasks'/t['task_id'].replace(':', '__')/'status.json').exists()],
            new_tokens=after['totals']['total_tokens']-
                       batch.read(directory/'baseline.json')['totals']['total_tokens']))
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
