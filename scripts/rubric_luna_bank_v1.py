"""Deterministic unified bank export; manifest is published last."""
import collections
import hashlib
import json
from pathlib import Path

import rubric_luna_batch03 as b


def sha_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def accepted_record(task,record,directory):
    """Only replay the frozen score validator; never assign semantic scores."""
    folder = directory/'tasks'/task['task_id'].replace(':','__')
    state = b.read(folder/'status.json')
    assert state['status']=='passed' and len(state['rounds'])<=2
    revision = state['accepted_revision']
    assert revision in (0,1) and revision==len(state['rounds'])-1
    final = folder/f'round_{revision}'
    rubric = b.read(final/'rubric.json')
    assert rubric==b.read(record['accepted_ref'])
    source = b.read(task['original_ref'])
    audit = b.read(task['audit_ref'])
    role = 'generate' if revision==0 else 'revise'
    selection = b.read(final/role/'output.json')
    assert not b.legacy.base.validate_selection(selection,source,audit)
    assert b.legacy.base.compile_rubric(selection,source,audit)==rubric
    judge = b.read(final/'judge/output.json')
    verdict = b.legacy.base.evaluate(judge,task['task_id'],
        {r['id'] for r in rubric['rubric']},{c['candidate_id'] for c in audit['candidates']})
    assert verdict['status']=='passed' and verdict==b.read(final/'evaluation.json')
    threads = []
    for rev in range(revision+1):
        rd = folder/f'round_{rev}'
        for role in ('generate' if rev==0 else 'revise','judge'):
            usage = b.read(rd/role/'usage.json') if (rd/role/'usage.json').exists() else None
            # A structure-invalid original generation has no first judge.
            if usage is None:
                assert rev==0 and role=='judge' and b.read(rd/'validation.json')['errors']
                continue
            assert usage['model']==b.MODEL and usage['reasoning_effort']==b.EFFORT
            assert usage['actual_config_verified'] and not usage['usage_unknown']
            threads.append(usage['thread_id'])
    assert len(threads)==len(set(threads))
    return dict(dataset_id=record['dataset_id'],task_id=task['task_id'],run_id=directory.name,
        rubric=rubric,source_artifact_ref=record['accepted_ref'],
        source_artifact_sha256=b.digest(record['accepted_ref']),judge_ref=str(final/'judge/output.json'),
        judge_sha256=b.digest(final/'judge/output.json'),model=b.MODEL,reasoning_effort=b.EFFORT,
        actual_usage_refs=record['actual_usage_refs'])


def publish():
    b.configure()
    with b.legacy.locked(b.REGISTRY/'.lock'):
        batches,records = b.legacy.replay()
        directories = b.HISTORY+[b.RUN]
        manifests = {d.name:b.legacy.execution_manifest(d)[0] for d in directories}
        expected = set()
        for directory in directories:
            manifest = manifests[directory.name]
            b.legacy.verify(manifest,directory,implementations=False)
            ids = {(manifest.get('dataset_id',b.legacy.DATASET_ID),t['task_id']) for t in manifest['tasks']}
            assert not expected & ids
            expected |= ids
        assert len(expected)==sum(m['sample_size'] for m in manifests.values())
        selected = [r for r in records.values() if r['run_id'] in manifests]
        assert len(selected)==len(expected)
        assert {(r['dataset_id'],r['task_id']) for r in selected}==expected
        assert all(r['status'] in b.legacy.TERMINAL for r in selected)
        # Derived publication links do not change content identity on re-export.
        identity_batches = {k:{a:v for a,v in batches[k].items() if a not in
            ('bank_manifest_ref','bank_manifest_sha256','bank_export_ref')} for k in manifests}
        identity = sha_text(encode(dict(batches=identity_batches,tasks=sorted(selected,key=lambda r:r['task_id']))))
        entry = b.REGISTRY/'bank_manifest.json'
        if entry.exists():
            old = b.read(entry)
            if old.get('content_identity_sha256')==identity:
                for item in old['exports'].values():
                    assert b.digest(item['path'])==item['sha256']
                guidance()
                return old
        version = b.REGISTRY/'exports'/identity
        b.durable_mkdir(version)
        bank,accepted,states,known,unknown = [],[],collections.Counter(),0,[]
        lookup = {(d.name,t['task_id']):t for d in directories for t in manifests[d.name]['tasks']}
        all_threads,all_responses = set(),set()
        for directory in directories:
            usage = b.read(directory/'usage.json')
            assert b.legacy.usage_summary(usage['threads'])['totals']==usage['totals']
            for u in usage['threads']:
                if u.get('thread_id'):
                    assert u['thread_id'] not in all_threads
                    all_threads.add(u['thread_id'])
                for rid in u.get('response_usage',{}):
                    assert rid not in all_responses
                    all_responses.add(rid)
            known += usage['totals']['total_tokens']
            unknown += [dict(run_id=directory.name,usage=u) for u in usage['unknown_usage']]
        for record in sorted(selected,key=lambda r:(r['dataset_id'],r['task_id'])):
            task = lookup[(record['run_id'],record['task_id'])]
            directory = Path(batches[record['run_id']]['manifest_ref']).parent
            original = b.read(record['original_ref'])
            assert sha_text(original['task'])==task['task_sha256']==record['task_sha256']
            state = b.read(record['state_ref'])
            assert state['status']==record['status']
            passed = record['status']=='passed'
            assert bool(record['accepted_ref'])==passed
            for ref in record['actual_usage_refs']:
                u = b.read(ref)
                assert u['task_id']==record['task_id'] and u.get('run_id',directory.name)==record['run_id']
            judged = accepted_record(task,record,directory) if passed else None
            if judged:
                accepted.append(judged)
            bank.append(dict(dataset_id=record['dataset_id'],task_id=record['task_id'],
                scenario=record['scenario'],index=record['index'],task=original['task'],
                task_sha256=task['task_sha256'],original_ref=record['original_ref'],
                original_sha256=b.digest(record['original_ref']),sources=task['raw_sources'],
                run_id=record['run_id'],status=record['status'],passed=passed,
                final_rubric_ref=record['final_rubric_ref'],accepted_rubric_ref=record['accepted_ref'],
                judge_ref=judged['judge_ref'] if judged else record.get('final_judge_ref') or record.get('first_judge_ref'),
                judge_model=b.MODEL if judged else None,actual_usage_refs=record['actual_usage_refs'],
                unknown_usage_refs=record['unknown_usage_refs'],errors=record['errors']))
            states[record['status']] += 1
        assert len(accepted)==sum(r['status']=='passed' for r in selected)
        exports = {}
        for name,rows in [('question_bank',bank),('accepted_rubrics',accepted)]:
            path = version/(name+'.jsonl')
            b.freeze(path,''.join(encode(row)+'\n' for row in rows),raw=True)
            exports[name] = dict(path=str(path),sha256=b.digest(path),rows=len(rows))
        snapshots = {}
        for name in ('batches.json','tasks.jsonl','events.jsonl'):
            source = b.REGISTRY/name
            target = version/('registry_'+name)
            b.freeze(target,source.read_text(),raw=True)
            snapshots[name] = dict(path=str(target),sha256=b.digest(target))
        prior_version = b.read(version/'bank_manifest.json') if (version/'bank_manifest.json').exists() else None
        manifest = dict(exported_at=prior_version['exported_at'] if prior_version else b.now(),content_identity_sha256=identity,total_questions=len(bank),
            passed=len(accepted),statuses=dict(states),known_luna_tokens=known,unknown_usage=unknown,
            exports=exports,registry_snapshot=snapshots,
            batches=[dict(run_id=d.name,manifest_ref=str(d/'manifest.json'),
                          manifest_sha256=b.digest(d/'manifest.json')) for d in directories],
            source_file_hashes=manifests[b.RUN_ID]['source_file_hashes'],
            original_dataset_unmodified=True,all_frozen_tasks_terminal=True)
        b.freeze(version/'bank_manifest.json',manifest)
        # Publication boundary: readers follow this entry and verify immutable files.
        b.snapshot(entry,manifest)
        updates = []
        for run_id in manifests:
            record = dict(batches[run_id],bank_manifest_ref=str(entry),
                          bank_manifest_sha256=b.digest(entry),bank_export_ref=str(version))
            updates.append(record)
        b.legacy.commit('7000题一致版本题库发布；通过版仅收录合法Luna独立评分',updates)
        b.legacy.cumulative_report()
        guidance()
        return manifest


def guidance():
    path = b.REGISTRY/'README.md'
    text = path.read_text() if path.exists() else '# Luna系列登记\n'
    marker = '## batch03正式续跑与统一题库'
    if marker in text:
        return
    addition = '\n\n## batch03正式续跑与统一题库\n\n统一题库入口bank_manifest.json；按exports中的绝对路径和SHA256读取同一版本question_bank、accepted_rubrics。原题库保留所有状态，通过版只收录合法Luna独立评分。原数据不重写。\n\n按task_id查询tasks.jsonl；失败原因及原始调用见errors、state_ref、actual_usage_refs。\n\n原manifest续跑：agent-world-model/.venv/bin/python scripts/rubric_luna_batch03_production_v1.py run --acceptance runs/rubric_luna_high_3000_batch03/production_tests/acceptance.json。先核对旧进程、未知请求和唯一锁；终态不重跑、不重抽样。全部终态后finalize只做幂等导出，不调用模型。\n\n本批报告：runs/rubric_luna_high_3000_batch03/report_zh.md；累计报告：report_zh.md。主调度和历史未知用量单列。\n'
    b.atomic(path,text+addition)
