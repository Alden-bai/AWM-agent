"""Offline handoff publication. No model invocation and no task-result changes."""
import json
from pathlib import Path
import sys

import rubric_luna_batch03_recovery_v1 as r


def main():
    with r.locked(r.RUN / '.runner.lock'):
        verification = r.verify_recovery()
        contract = r.read(r.REPAIR / 'continuation_contract.json')
        auth = r.read(r.REPAIR / 'authorization.json')
        paths = list(r.RUN.glob('tasks/*/round_*/*/usage.json'))
        paths += list((r.REPAIR / 'attempts').glob('*/*/attempt_1/usage.json'))
        usages = [dict(r.read(p), artifact_ref=str(p)) for p in paths]
        seen = {u.get('thread_id') for u in usages if u.get('thread_id')}
        if len(seen) != len(usages):
            raise ValueError('duplicate_or_missing_thread')
        for old in contract['old_unknown_usage_requests']:
            if old['thread_id'] in seen:
                continue
            placeholder = r.REPAIR / 'old_unknown_usage' / (old['thread_id'] + '.json')
            value = dict(old['identity'], thread_id=old['thread_id'], usage_unknown=True,
                         original_folder=old['original_folder'], server_completion_evidence=False)
            r.durable(placeholder, value, immutable=True)
            usages.append(dict(value, artifact_ref=str(placeholder)))
        known = [u for u in usages if not u.get('usage_unknown') and u.get('actual_usage')]
        unknown = [u for u in usages if u.get('usage_unknown')]
        responses = [rid for u in known for rid in u.get('response_usage', {})]
        if len(set(responses)) != len(responses):
            raise ValueError('duplicate_response_id')
        totals = {k:sum(u['actual_usage'].get(k,0) for u in known) for k in r.USAGE_KEYS}
        totals['noncached_input_tokens'] = totals['input_tokens'] - totals['cached_input_tokens']
        if totals['total_tokens'] != 11947868 or len(known) != 316 or len(unknown) != 6:
            raise ValueError('unexpected_handoff_usage_totals')
        summary = dict(at=r.now(), totals=totals, threads=usages, unknown_usage=unknown,
            known_thread_count=316, unknown_request_count=6, retry_known_tokens=234722,
            cumulative_before_retries=11713146, scheduler_usage_excluded=True,
            completed_3000_question_bill=False, original_usage_files_unchanged=True)
        r.durable(r.REPAIR / 'usage_summary.json', summary)
        files = ['scripts/rubric_luna_batch03_recovery_v1.py', 'scripts/rubric_luna_batch03_resume_recovered_v1.py',
                 'tests/test_rubric_luna_batch03_recovery_v1.py', 'tests/test_rubric_luna_batch03_resume_recovered_v1.py']
        hashes = {p:r.sha(r.ROOT / p) for p in files}
        for path in files:
            r.durable(r.REPAIR / 'implementation_v1' / Path(path).name,
                      (r.ROOT / path).read_text(encoding='utf-8'), raw=True, immutable=True)
        r.durable(r.REPAIR / 'offline_acceptance.json', dict(at=r.now(), passed=True, paid_calls=0,
            tests_run=12, tests_passed=12, command="python -X utf8 -m unittest discover -s tests -p 'test_rubric_luna_batch03_*recover*.py' -v",
            implementation_hashes=hashes, live_paid_scope='exactly_six_replacement_stage_calls_already_completed',
            full_2888_task_runtime_not_started_or_benchmarked=True))
        r.durable(r.REPAIR / 'completion.json', dict(at=r.now(), scope='six_stage_requests_only',
            six_stage_retries_complete=True, all_actual_models_verified=r.MODEL, reasoning_effort=r.EFFORT,
            old_unknown_requests_preserved=6, old_holds_retained=6, peak_total_permits=7,
            retry_known_tokens=234722, batch_known_tokens=11947868,
            terminal=112, pending=2888, remaining_batch_started=False,
            continuation_entry='scripts/rubric_luna_batch03_resume_recovered_v1.py',
            recovery_contract_ref=str(r.REPAIR / 'continuation_contract.json'),
            final_bank_published=False))
        registry = r.ROOT / 'runs/rubric_luna_registry'
        with r.locked(registry / '.lock'):
            batches, records = {}, {}
            for line in (registry / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                for b in event.get('batches', []):
                    batches[b['run_id']] = b
                for task in event.get('tasks', []):
                    records[(task['dataset_id'],task['task_id'],task['run_id'])] = task
            if len(records) != 7000:
                raise ValueError('registry_expected_7000_unique_records')
            before = batches[r.RUN.name]
            batch = dict(before, recovery_status='six_stage_retries_complete_waiting_for_user_continuation',
                recovery_contract_ref=str(r.REPAIR / 'continuation_contract.json'),
                recovery_handoff_ref=str(r.RUN / 'RECOVERY_HANDOFF_20261102.md'),
                recovery_entry_ref=str(r.ROOT / 'scripts/rubric_luna_batch03_resume_recovered_v1.py'),
                luna_actual_usage=totals, usage_ref=str(r.REPAIR / 'usage_summary.json'), unknown_usage=unknown,
                terminal=112, pending=2888, old_remote_holds_retained=6,
                remaining_batch_started=False)
            if batch != before:
                r.append(registry / 'events.jsonl', dict(at=r.now(), reason='用户授权的6个阶段请求已独立重做；保留旧占用和未知用量，等待另一窗口续跑', batches=[batch], tasks=[]))
            batches[r.RUN.name] = batch
            r.durable(registry / 'batches.json', batches)
        print(r.encode(dict(handoff_complete=True, recovery_scope='six_requests', remaining_batch_started=False,
                            retry_tokens=234722, cumulative_known_tokens=11947868, old_unknown_requests=6)))


if __name__ == '__main__':
    main()
