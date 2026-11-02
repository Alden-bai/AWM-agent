"""Offline comparison of frozen v12 outputs; does not call either provider."""
import json
from pathlib import Path

from rubric_pilot import file_hash, read_json, write_json
from rubric_qwen_deepseek_v12 import RUN, ROOT


def main():
    m = read_json(RUN / 'manifest.json')
    c = read_json(RUN / 'cost_report.json')
    audit = read_json(RUN / 'source_review.json')
    judge_audit = read_json(RUN / 'judge_review.json')
    assert len(audit['tasks']) == len(c['tasks']) == 10
    unknown = []
    for path in sorted((RUN / 'deepseek').glob('*/request_identity.json')):
        if (path.parent / 'response.json').exists():
            continue
        identity = read_json(path)
        req = read_json(path.parent / 'planned_request.json')
        assert file_hash(path.parent / 'planned_request.json') == identity['request_sha256']
        assert identity['reservation_upper'] < 128000
        output = req['max_tokens']
        # Request occurred on Saturday: off-peak. Assume no cache discount;
        # include the estimator's safety margin in input for a conservative reserve.
        rate = m['pricing']['deepseek']['off_peak']
        reserve_cost = ((identity['reservation_upper'] - output) * rate['miss']
                        + output * rate['output']) / 1e6
        unknown.append(dict(task_id=path.parent.name.replace('__', ':'),
            reserved_tokens=identity['reservation_upper'],
            conservative_reserved_cost_cny=reserve_cost,
            actual_usage=None, actual_bill=None, automatically_retried=False))
    assert sum(x['reserved_tokens'] for x in unknown) == c['unknown_reserved_tokens']
    for b in m['baseline_refs'].values():
        assert file_hash(b['path']) == b['sha256']
    for row in audit['tasks']:
        if row.get('rubric_sha256'):
            assert row['rubric_sha256'] == file_hash(
                RUN / 'compiled' / row['task_id'].replace(':', '__') / 'rubric.json')
    s = c['summary']
    a = {x['task_id']: x for x in audit['tasks']}
    dims = m['evaluation']['dimensions']
    compared = []
    for row in c['tasks']:
        if 'judge' in row:
            j = row['judge']
            compared.append(dict(task_id=row['task_id'], judge_status=j['status'],
                independent_source_status=a[row['task_id']]['status'],
                blocked_by_valid_zero=j['status'] == 'rejected'))
    qwen_raw = sum((RUN / 'qwen' / t['task_id'].replace(':', '__') / 'output.json').exists()
                   for t in c['tasks'])
    comparison = dict(
        scope='Same ten known development tasks; no paired historical DeepSeek review; no runtime execution',
        qwen_complete_intermediate_json=qwen_raw,
        compiled_five_field_rubrics=s['compiled'],
        independent_source_provisionally_usable=audit['summary']['static_semantic_provisionally_usable'],
        judge_vs_independent_source=compared,
        v11_cost_cny=0.372685,
        v12_cost_cny=c['totals']['estimated_cny'],
        v12_cost_basis='Confirmed usage only; unknown request separately reserved, not treated as zero',
        unknown_requests=unknown,
        conservative_cost_including_unknown_reserve_cny=(c['totals']['estimated_cny'] +
            sum(x['conservative_reserved_cost_cny'] for x in unknown)),
        cost_change_vs_v11_percent=(c['totals']['estimated_cny'] / 0.372685 - 1) * 100,
        strict_contract_comparability='v12 fixed fields/quotes assembled by code; v11 raw model contract',
        quality_preference_validation='All compiled items required in this sample; quality path unit-tested only',
        no_tasks_deleted=True,
        judge_reason_audit_ref=str(RUN / 'judge_review.json'),
        truncated_reviews=[r['task_id'] for r in c['requests']
                           if r['stage'] == 'deepseek' and r['finish_reason'] == 'length'],
    )
    write_json(RUN / 'comparison.json', comparison)
    text = [
        '# v12：代码候选 + Qwen编译 + DeepSeek Flash独立评分', '',
        f"同一10题：Qwen返回{qwen_raw}份完整中间JSON，{s['compiled']}份通过映射校验并装配为五字段rubric；"
        f"DeepSeek完成{s['judge_completed']}份评分，{s['rejected']}份因至少一个0分淘汰，"
        f"{s['review']}份有效评分进入待复核，{s['eligible']}份全2分；"
        f"另有{s['compiled'] - s['judge_completed']}份审查未完整完成，不算通过。10道原题全部保留。", '',
        '独立静态来源复核：8份已装配rubric均有阻断问题，可暂时保留0/10。'
        '因此本轮证明了来源绑定、格式装配和淘汰机制可运行，没有证明最终语义交付率提升。'
        '独立复核中的反例是假设案例，没有执行真实环境。', '',
        '## 方案落实', '',
        '代码提取43个候选，覆盖题面所有非空白字符，保留完整题面及Unicode起止位置。'
        'Qwen筛选、合并、区分required/quality并编写业务要求和G/P；程序只装配原文引用及固定三值判定。'
        '每条除id外仍为criterion、sources、requirement_level、evidence、decision五字段。'
        'public_contract用于解释接口能力，不能替题面创造要求。', '',
        'DeepSeek每题使用新的system+user上下文，只接收本题公开来源、候选筛选表和最终rubric，'
        '不接收Qwen思考过程、旧评分、独立审查参考或其他题。'
        '任一有效维度0分即淘汰该版rubric；全2分才进入候选可用状态；含1分或无效审查待复核。'
        '淘汰采用状态标记并保留审计产物，不删除原题或掩盖失败输出。', '',
        '全部已装配条目为required；这10题没有验证明确quality偏好的端到端筛选能力，'
        '该分支仅有单元测试，未强行生成质量偏好。', '',
        '## 同题对比', '',
        '| 指标 | v10思考 | v11 | v12 |',
        '|---|---:|---:|---:|',
        f'| 可解析最终rubric | 10 | 6 | {s["compiled"]} |',
        f'| 原文引用及五字段严格契约通过 | 6 | 3 | {s["compiled"]}（代码装配） |',
        '| 新增候选/义务映射也通过 | 未设 | 1 | 8 |',
        '| 独立静态语义暂可保留 / 10题 | 0 | 0 | 0 |',
        f'| 实际API请求 | 10 | 22 | {c["physical_requests"]} |',
        f'| 已确认tokens | 177,805 | 405,992 | {c["totals"]["total_tokens"]:,} |',
        f'| 用量未知请求的预留tokens | 0 | 0 | {c["unknown_reserved_tokens"]:,} |',
        f'| 已确认用量估算费（人民币） | ¥0.240603 | ¥0.372685 | ¥{c["totals"]["estimated_cny"]:.6f} + 未知请求 |', '',
        'v12的10份Qwen输出是中间表示，不能与旧版最终rubric直接比较格式成功率；'
        '来源绑定和固定判定现在由程序保证。旧版没有相同DeepSeek五维评分，不能声称评分提升。'
        '本轮同时改变了流程、生成长度及审查模型，且使用已知开发集，不是单因素消融或泛化评估。', '',
        '## 五维评分与版本去向', '',
        '维度顺序：来源支持 / 题意覆盖 / 硬软分类 / 证据设计 / 判定逻辑。', '',
        '| 题目 | 五维0–2分 | 版本去向 | 静态来源复核 |',
        '|---|---|---|---|',
    ]
    for row in c['tasks']:
        j = row.get('judge', {})
        scores = ' / '.join(str(j.get('scores', {}).get(d, '—')) for d in dims)
        text.append(f"| {row['task_id']} | {scores} | {j.get('status', row['state']['status'])} | {a[row['task_id']]['status']} |")
    text += ['', '模型的评分是审查建议。即使淘汰结论与独立复核一致，也不代表每项理由正确，'
             '更不代表运行准确率；该样本缺少独立复核认为合格的版本，不能估计误杀率。', '',
             '未完整评分的版本不算通过。审查截断：' +
             ('、'.join(comparison['truncated_reviews']) or '无') +
             '。截断请求消耗仍计入总费用，不解析残缺分数或自动重试。', '',
             '### 独立复核审查理由', '']
    for row in judge_audit['tasks']:
        text += [f"- {row['task_id']}：{row['conclusion']}"]
        for issue in row.get('incorrect_or_unsupported_findings', []):
            text += [f"  理由边界：{issue}"]
    text += ['',
             '## 分阶段费用及128k约束', '',
             '| 阶段 | 请求 | 输入tokens | 输出tokens（含思考） | 估算人民币 |',
             '|---|---:|---:|---:|---:|']
    for stage, values in c['stages'].items():
        responses = sum(r['stage'] == stage for r in c['requests'])
        attempts = responses + (len(unknown) if stage == 'deepseek' else 0)
        text.append(f"| {stage} | {attempts}次请求/{responses}份usage | {values['prompt_tokens']:,} | {values['completion_tokens']:,} | ¥{values['estimated_cny']:.6f}（已确认用量） |")
    text += ['', f"仅已确认用量估算，相较v11费用已变化{comparison['cost_change_vs_v11_percent']:+.1f}%。费用包含本轮生成和独立审查，"
             '没有付费重试或修复。Qwen按输入¥0.2/百万、输出¥2/百万估算；DeepSeek按实际响应时间及缓存命中分项估算，'
             '本轮空闲时段为缓存命中¥0.02/百万、未命中¥1/百万、输出¥4/百万。', '',
             '价格来源：[Qwen官方北京定价](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)、'
             '[DeepSeek官方人民币定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。'
             '估算不是实付账单，不含Codex开发及独立复核用量；输出已含思考，未重复计费。', '',
             f"单次最大实际输入{c['max_actual_input_tokens']:,} tokens；"
             f"最大实际总tokens {max(x['total_tokens'] for x in c['requests']):,}；"
             f"输入保守估算+输出/思考预留+余量的最大值{c['max_conservative_reservation']:,}，均小于128000。"
             f"本轮未知预留{c['unknown_reserved_tokens']:,}；共享账本所有未知预留{c['cumulative_unknown_reserved']:,}仍保留。", '',
             f"连接失败请求的实际服务端用量未知，未重试、未按0费用抹去；按输入保守估算及输出上限预留"
             f"¥{sum(x['conservative_reserved_cost_cny'] for x in unknown):.6f}，"
             f"已确认费用加该预留合计¥{comparison['conservative_cost_including_unknown_reserve_cny']:.6f}。"
             '这是预算口径，不是已确认用量或实付账单。', '',
             '## 逐题独立来源复核', '']
    for row in audit['tasks']:
        text += ['### ' + row['task_id'], '', row['notes'], '']
        for f in row['findings']:
            text += [f"- {f['item_id']}：{f['issue']} 假设反例：{f['counterexample']}"]
        text += ['']
    text += ['## 验证与下一步边界', '',
             '14项相关单元测试通过。汇总离线重建每题规范输入及装配rubric，核验原文偏移、'
             '引用哈希、实际请求与冻结请求、响应解析与保存结果、共享账本usage及128k预留。'
             '原题/候选、模型原始输出、五字段成品、独立审查、版本去向及费用分开保存。', '',
             '后续优化应优先处理集合等式、G仅代表可判定性、条件执行和歧义保留；'
             '同时简化双向映射的模型输出负担。这些是本轮发现，不在本轮偷偷修补或追加收费复跑。', '']
    (RUN / 'report_zh.md').write_text('\n'.join(text))
    print(json.dumps(comparison, ensure_ascii=False))


if __name__ == '__main__':
    main()
