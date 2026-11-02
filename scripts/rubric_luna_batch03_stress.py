"""Explicit 12-concurrency pressure test of the existing frozen batch03 queue.

The frozen production baseline remains 3. Only this independently recorded,
bounded test overrides the gate to 12. Business prompts, request identities,
cache recovery, stage dependencies and process cleanup are unchanged.
"""
import argparse
import asyncio
import copy
import datetime
import json
from pathlib import Path

import rubric_luna_batch03 as batch
from rubric_luna_dispatch import Gate


class StressGate(Gate):
    def __init__(self,persist,limit=3,**kwargs):
        super().__init__(persist,limit=12,**kwargs)

    def state(self):
        value = super().state()
        value.update(initial_concurrency=12,production_initial_concurrency=3,
                     diagnostic_override=True,promotion_qualification_claimed=False)
        return value


def enrich_record(original,m,task,directory,run_id,state=None):
    rec = original(m,task,directory,run_id,state)
    folder = directory/'tasks'/task['task_id'].replace(':','__')
    rubrics = sorted(folder.glob('round_*/rubric.json'))
    if rubrics and rec.get('final_rubric_ref') is None:
        path = rubrics[-1]
        rec.update(final_version=int(path.parent.name.split('_')[1]),final_rubric_ref=str(path),
                   final_rubric_sha256=batch.digest(path))
    if rec['status'] not in batch.legacy.TERMINAL and rubrics:
        revision = int(rubrics[-1].parent.name.split('_')[1])
        evaluation = rubrics[-1].parent/'evaluation.json'
        if evaluation.exists() and batch.read(evaluation)['status']=='rejected' and revision==0:
            rec['next_stage']='one_revision_generation'
        else:
            rec['next_stage']='initial_judge' if revision==0 else 'fresh_final_judge'
    return rec


async def execute(m,seconds=180,max_calls=13):
    original_gate,original_record = batch.Gate,batch.legacy.task_record
    batch.Gate = StressGate
    batch.legacy.task_record = lambda *args,**kwargs: enrich_record(original_record,*args,**kwargs)
    try:
        trial = batch.Trial(m,seconds=seconds,max_calls=max_calls)
        summary = await trial.run()
        summary.update(initial_concurrency=12,production_initial_concurrency=3,
                       diagnostic_override=True,promotion_eligible=False)
        batch.snapshot(batch.RUN/'trial_summary.json',summary)
        authorization = batch.read(batch.RUN/'trial_authorization.json')
        authorization.update(initial_concurrency=12,production_initial_concurrency=3,
                             purpose='explicit user requested 12-concurrency pressure test',
                             promotion_qualification_claimed=False)
        batch.snapshot(batch.RUN/'trial_authorization.json',authorization)
        batch.report(m)
        return summary
    finally:
        batch.Gate,batch.legacy.task_record = original_gate,original_record


def freeze_execution(test_dir):
    initial = batch.read(batch.RUN/'manifest.json')
    chain = batch.legacy.execution_manifest(batch.RUN)[1]
    execution = copy.deepcopy(batch.read(chain[-1]['path']))
    version = len(chain)
    execution.update(original_frozen_manifest_ref=str(batch.RUN/'manifest.json'),
                     original_frozen_manifest_sha256=batch.digest(batch.RUN/'manifest.json'),
                     previous_execution_manifest_ref=chain[-1]['path'],
                     created_for_execution_at=batch.now(),
                     diagnostic_override=dict(actual_initial_concurrency=12,
                         production_initial_concurrency=3,max_inflight=12,max_new_calls=13,
                         dispatch_seconds=180,user_instruction='帮我验证一下12并发',
                         record_ref=str(test_dir),automatic_promotion=False))
    execution['implementation_hashes'].update({str(Path(__file__)):batch.digest(__file__),
          str(batch.ROOT/'tests/test_rubric_luna_batch03_stress.py'):
              batch.digest(batch.ROOT/'tests/test_rubric_luna_batch03_stress.py')})
    path = batch.RUN/f'execution_manifest_v{version}.json'
    batch.freeze(path,execution)
    verified,_ = batch.legacy.execution_manifest(batch.RUN)
    batch.legacy.verify(verified)
    assert verified['tasks']==initial['tasks']
    with batch.legacy.locked(batch.REGISTRY/'.lock'):
        bs,_ = batch.legacy.replay()
        record = batch.legacy.batch_record(verified,bs[batch.RUN_ID])
        record['diagnostic_override'] = execution['diagnostic_override']
        batch.legacy.commit('用户明确要求12并发压力测试；冻结独立执行修订链，不改变原manifest',[record])
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--test-id',required=True)
    args = parser.parse_args()
    if not args.test_id.replace('_','').replace('-','').isalnum():
        parser.error('test-id must contain only letters, digits, underscores or hyphens')
    batch.configure()
    with batch.legacy.locked(batch.RUN/'.runner.lock',blocking=False):
        m = batch.prepare()
        batch.orphan_check()
        state = batch.read(batch.RUN/'scheduler_state.json')
        if state.get('active') or state.get('remote_holds'):
            raise RuntimeError('unresolved_request_blocks_pressure_test')
        if state.get('stop_reason') not in (None,'trial_call_limit','trial_dispatch_deadline','user_stop'):
            raise RuntimeError('prior_failure_requires_reconciliation')
        directory = batch.RUN/'stress_tests'/args.test_id
        if (directory/'summary.json').exists() or (directory/'started.json').exists():
            raise RuntimeError('test_already_started_no_automatic_repeat')
        if not (directory/'baseline.json').exists():
            for name in ('report_zh.md','trial_summary.json','trial_analysis.json','scheduler_state.json',
                         'trial_authorization.json','usage.json','results.json','progress.json','final_verification.json'):
                source = batch.RUN/name
                if source.exists():
                    batch.freeze(directory/'before'/name,source.read_text(),raw=True)
            prior_calls = {str(p):batch.digest(p) for p in batch.RUN.glob('tasks/*/round_*/*/usage.json')}
            batch.freeze(directory/'baseline.json',dict(at=batch.now(),usage_refs=prior_calls,
                         totals=batch.read(batch.RUN/'usage.json')['totals'],
                         original_manifest_sha256=batch.digest(batch.RUN/'manifest.json')))
            path = freeze_execution(directory)
            batch.freeze(directory/'authorization.json',dict(user_instruction='帮我验证一下12并发',
                max_new_calls=13,dispatch_seconds=180,drain_seconds=60,global_max=12,
                actual_initial_concurrency=12,production_initial_concurrency=3,
                execution_manifest_ref=str(path),recorded_at=batch.now()))
        if args.prepare_only:
            print(json.dumps(dict(prepared=str(directory),max_new_calls=13,actual_concurrency=12)),flush=True)
            return
        # Explicitly verify the adapter too, not just the original frozen entry.
        latest,_ = batch.legacy.execution_manifest(batch.RUN)
        batch.legacy.verify(latest)
        batch.freeze(directory/'started.json',dict(at=batch.now()))
        result = asyncio.run(execute(m))
        batch.freeze(directory/'summary.json',result)
        batch.freeze(directory/'after_usage.json',batch.read(batch.RUN/'usage.json'))
        print(json.dumps({k:v for k,v in result.items() if k!='suspended'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
