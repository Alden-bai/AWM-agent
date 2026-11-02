"""Final accounting and file/hash audit only; never inspects rubric semantics."""
import collections
import json
from pathlib import Path

import rubric_luna100 as run


def main():
    m = run.manifest()
    run.verify_manifest(m)
    states = {s['task_id']: s for s in (run.read(p) for p in (run.RUN/'tasks').glob('*/status.json'))}
    assert set(states) == {t['task_id'] for t in m['tasks']}
    assert len(states) == m['sample_size'] == 100
    retained = []
    for task in m['tasks']:
        for source in task['raw_sources'].values():
            assert run.digest(source['path']) == source['sha256']
        if task['preparation_status'] == 'ready':
            original = run.read(task['original_ref'])
            assert run.hashlib.sha256(original['task'].encode()).hexdigest() == task['task_sha256']
        else:
            assert Path(task['retained_task_ref']).exists()
        state = states[task['task_id']]
        assert len(state['rounds']) <= 2
        accepted = run.RUN/'accepted'/(task['task_id'].replace(':', '__')+'.json')
        assert accepted.exists() == (state['status'] == 'passed')
        retained.append(dict(task_id=task['task_id'], original_retained=True, hashes_verified=True, state=state['status']))
    run.report()
    usage = run.read(run.RUN/'usage.json')
    threads = usage['threads']
    assert all(t['actual_config_verified'] for t in threads if not t.get('usage_unknown'))
    assert all(t['per_response_sum_matches_cumulative'] for t in threads if not t.get('usage_unknown'))
    assert len({t['thread_id'] for t in threads if t.get('thread_id')}) == sum(bool(t.get('thread_id')) for t in threads)
    assert usage['totals']['input_tokens'] + usage['totals']['output_tokens'] == usage['totals']['total_tokens']
    for stage, detail in usage['stages'].items():
        selected = [t for t in threads if t['stage'] == stage]
        detail.update(model_response_count=sum(t.get('model_response_count', 0) for t in selected),
                      tool_call_count=sum(t.get('tool_call_count', 0) for t in selected),
                      unknown_usage_calls=sum(t.get('usage_unknown', False) for t in selected),
                      noncached_input_tokens=detail['input_tokens']-detail['cached_input_tokens'])
    usage['source_preparation_method'] = 'All public contracts/tool inventory/table and index DDL; remove only confirmed schema-descriptor example metadata (any JSON value type), preserve real example/examples fields; original JSON token offsets; outer whitespace compaction only.'
    usage['launcher_pre_call_failure'] = dict(model_calls=0, actual_tokens=0, ref=str(run.RUN/'launcher_failures/config_preflight_before_model/verified_no_model_call.json'))
    run.snapshot(run.RUN/'usage.json', usage)
    results = run.read(run.RUN/'results.json')
    results['stages'] = usage['stages']
    run.snapshot(run.RUN/'results.json', results)
    run.snapshot(run.RUN/'retention_manifest.json', dict(tasks=retained, frozen_manifest_sha256=run.digest(run.RUN/'manifest.json'), semantic_review_performed=False))
    run.snapshot(run.RUN/'accepted_manifest.json', [dict(task_id=t['task_id'], path=str(run.RUN/'accepted'/(t['task_id'].replace(':', '__')+'.json'))) for t in retained if t['state'] == 'passed'])
    report = run.RUN/'report_zh.md'
    with report.open('a') as stream:
        stream.write('\n执行方式对照：本轮用CLI全新会话的消息模式，旧10题用协作子agent的文件读写模式；上下文组织及工具开销也有变化，token差异不能单独归因于业务prompt，也不能据此证明质量保持。\n')
        stream.write('\n来源压缩方法补充：descriptor的example/examples元数据可为标量、数组或对象，确认属于元数据后才移除；真实同名业务参数和字段保留。audit中的方法简写“scalar”不限定被移除示例值的JSON类型。\n')
        stream.write('\n确定性核验：100个冻结task_id与100份最终状态一致；原题和来源哈希核对通过；最多一轮修订；通过文件与状态一一对应；模型配置及逐响应/累计用量全部核对。没有进行额外语义复核。\n')
        stream.write('\n启动时一次CLI配置解析失败发生在创建线程和模型请求之前，确认模型调用及token均为0；另一次生成完成后的记账错误已从原响应离线恢复，没有重新发送生成请求。两次技术记录均保留，manifest修改链保留原抽样、来源、prompt和配置。\n')
    print(json.dumps(dict(audited_tasks=100, actual_tokens=usage['totals']['total_tokens'], unknown_usage_calls=len(usage['unknown_usage'])), ensure_ascii=False))


if __name__ == '__main__':
    main()
