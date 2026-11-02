"""Offline reconciliation of the frozen, authorized ten-task v7 input-only pilot."""
import copy
import json
import sqlite3

from estimate_rubric_v7_input_only import prune_examples
from rubric_pilot import file_hash, read_json, write_json
from rubric_v7_input_only import BASE, RUN, signature


def main():
    manifest = read_json(RUN / 'manifest.json')
    review = read_json(RUN / 'initial/source_review.json')
    by_id = {t['task_id']: t for t in review['tasks']}
    rows = []
    with sqlite3.connect(BASE / 'budget.sqlite') as db:
        db.row_factory = sqlite3.Row
        ledger = [dict(r) for r in db.execute('select * from requests')]
    for t in manifest['tasks']:
        key = t['task_id'].replace(':', '__')
        folder = RUN / 'initial' / key
        original = read_json(BASE / 'v7' / key / 'request.json')
        request = read_json(folder / 'request.json')
        assert request == read_json(folder / 'planned_request.json')
        assert file_hash(folder / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert request['messages'][0] == original['messages'][0]
        assert {k: v for k, v in request.items() if k != 'messages'} == {
            k: v for k, v in original.items() if k != 'messages'}
        expected = copy.deepcopy(json.loads(original['messages'][1]['content']))
        for source in expected['sources']:
            if source['kind'] == 'public_contract':
                source['text'] = json.dumps(prune_examples(json.loads(source['text'])), ensure_ascii=False)
        assert expected['task'] == next(s['text'] for s in expected['sources'] if s['kind'] == 'task')
        del expected['task']
        assert expected == json.loads(request['messages'][1]['content'])
        identity = read_json(folder / 'request_identity.json')
        assert identity['signature'] == signature(request, t['input_sha256']) == t['cache_signature']
        records = [r for r in ledger if r['request_key'] == identity['request_key']]
        assert len(records) == 1 and records[0]['status'] == 'response'
        usage = read_json(folder / 'response.json')['usage']
        assert json.loads(records[0]['usage']) == usage
        assert records[0]['reserved'] == usage['total_tokens']
        assert usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens']
        rows.append(dict(task_id=t['task_id'], prompt_tokens=usage['prompt_tokens'],
                         completion_tokens=usage['completion_tokens'], total_tokens=usage['total_tokens'],
                         cache_hit_tokens=usage.get('prompt_cache_hit_tokens', 0),
                         review_status=by_id[t['task_id']]['status'],
                         identical_to_v7_json=by_id[t['task_id']]['identical_to_v7_json']))
    assert file_hash(BASE / 'v7/source_review.json') == manifest['baseline_review_sha256']
    assert file_hash(BASE / 'review_reference.json') == manifest['review_reference_sha256']
    current = [r for r in ledger if r['request_key'].startswith('v7_input_only/')]
    assert len(current) == 10 and all(r['status'] == 'response' for r in current)
    assert not any(r['status'] == 'in_flight' for r in ledger)
    total = {k: sum(t[k] for t in rows) for k in (
        'prompt_tokens', 'completion_tokens', 'total_tokens', 'cache_hit_tokens')}
    confirmed = sum(json.loads(r['usage'])['total_tokens'] for r in ledger if r['status'] == 'response')
    unknown = sum(r['reserved'] for r in ledger if r['status'] != 'response')
    cumulative = sum(r['reserved'] for r in ledger)
    assert confirmed + unknown == cumulative
    assert cumulative - manifest['budget_before']['tokens_accounted_or_reserved'] == total['total_tokens']
    assert total['total_tokens'] <= 160000 and cumulative <= 954558
    cost = dict(model=manifest['model'], base_url=manifest['base_url'], physical_requests=10,
                initial=total, repair_requests=0, repair_tokens=0, experiment_cap=160000,
                experiment_unused_tokens=160000-total['total_tokens'], cumulative_confirmed_tokens=confirmed,
                historical_unknown_reserved_tokens=unknown, cumulative_accounted_tokens=cumulative,
                cumulative_cap=954558, cumulative_physical_requests=len(ledger),
                baseline_v7={'prompt_tokens': 85701, 'completion_tokens': 24689, 'total_tokens': 110390},
                total_saving_tokens=110390-total['total_tokens'],
                total_saving_percent=100*(110390-total['total_tokens'])/110390,
                currency_cost=None, codex_audit_usage_included=False, tasks=rows)
    write_json(RUN / 'cost_report.json', cost)
    status = dict(state='completed_stopped_at_quality_gate', full_run=False, paid_repairs=0,
                  summary=review['summary'], remaining_budget_is_not_a_dispatch_instruction=True)
    write_json(RUN / 'status.json', status)
    lines = [
        '# v7 约束不变、只压缩输入：同10题试跑结果', '',
        '结果：7/10份草案暂可保留，未达到8/10且旧版通过题出现1题回退。按预定规则停止追加调用；未修复、未全量跑。', '',
        '## 实际用量', '',
        '| 阶段 | 输入tokens | 输出tokens | 合计tokens | 请求数 |',
        '|---|---:|---:|---:|---:|',
        '| 原v7单轮 | 85,701 | 24,689 | 110,390 | 10 |',
        f"| 本次首轮 | {total['prompt_tokens']:,} | {total['completion_tokens']:,} | {total['total_tokens']:,} | 10 |",
        '| 本次追加修复 | 0 | 0 | 0 | 0 |', '',
        f"本轮节省{cost['total_saving_tokens']:,} tokens（{cost['total_saving_percent']:.2f}%）；输入压缩贡献按输入用量比较为{85701-total['prompt_tokens']:,} tokens，输出长度差贡献为{24689-total['completion_tokens']:,} tokens。",
        f"本次16万额度剩余{cost['experiment_unused_tokens']:,}。累计账本占用{cumulative:,} = 已确认{confirmed:,} + 历史未知预留{unknown:,}，累计上限954,558。此前未知的2次请求继续占预算，本轮无未知请求。", '',
        f"缓存命中输入{total['cache_hit_tokens']:,} tokens已包含在输入总量，不能再次扣除。未核验实际账单价格，因此不报告人民币费用。这里仅统计DeepSeek API用量，不包含Codex审查用量。", '',
        '## 质量与逐题用量', '',
        '10/10结构及原始来源引用检查通过，共67条标准；7/10通过来源静态审查。原v7暂可保留8题中7题保留、1题回退；原有2题阻断问题仍在。5题完整JSON与v7一致。', '',
        '| 题目 | 输入 | 输出 | 合计 | 草案来源审查 | 与v7完整JSON一致 |',
        '|---|---:|---:|---:|---|---|',
    ]
    for t in rows:
        label = '暂可保留' if t['review_status'] == 'draft_review_pass_with_notes' else '需修改'
        lines.append(f"| {t['task_id']} | {t['prompt_tokens']:,} | {t['completion_tokens']:,} | {t['total_tokens']:,} | {label} | {'是' if t['identical_to_v7_json'] else '否'} |")
    lines += ['', '## 阻断问题', '']
    for t in review['tasks']:
        for finding in t['findings']:
            lines += [f"- **{t['task_id']} / {finding['item_id']}**：{finding['finding']}",
                      f"  纸面反例（未执行）：{finding['paper_counterexample']}"]
    lines += [
        '', '## 对照边界与下一步', '',
        '已逐项核验v7系统消息和模型参数完全一致、v6旧稿与反馈不变、完整schema和非example契约属性不变。仅删除example属性、重复题面和外层JSON空白。完整原始来源保留用于引用校验。', '',
        '本次回退发生在判定文字中：workflow_task_management_1:9 R02从“所得结果”退回“返回”，但证据段仍允许本地过滤。原有反馈也明确允许本地过滤；因此并非已证明某条业务规则被压缩器删掉。单次试跑不能区分模型输出波动与输入表达变化的因果影响。', '',
        '下一步建议保留原v7为参照，先离线增加三类反例验收：允许先取数据后本地过滤、同一对象的通过/失败互斥、已解析默认行为的分支覆盖。若另行试验，仅修复失败题，逐题冻结已通过输出，避免整组反复生成。当前未修改生成prompt、未派发额外请求。', '',
        '纯输入去重的单轮收益有限；相对历史多轮开发总量的减少不能全归因于压缩。本轮仍带v6旧稿及反馈，不代表一万道新题的首次生成成本或质量。', '',
        '7/10是同一批题的草案静态审查比例，不是检查器实测准确率。未执行这些纸面反例，未运行真实任务或验收检查器；暂可保留的草案仍保留原有业务歧义与未决证据条件。', '',
        f"[完整五字段rubric阅读版]({RUN / 'initial/rubrics_zh.md'}) · [逐题审查]({RUN / 'initial/source_review.json'}) · [用量明细]({RUN / 'cost_report.json'})", '',
        '复核命令（离线）：`agent-world-model/.venv/bin/python scripts/summarize_rubric_v7_input_only.py`。原始请求、响应、来源哈希和授权保存在本目录。',
    ]
    (RUN / 'report_zh.md').write_text('\n'.join(lines) + '\n')
    write_json(BASE / 'cost_followup.json', dict(cap=954558, cumulative_accounted_tokens=cumulative,
               historical_reports_frozen=True, latest_cost_report=str(RUN / 'cost_report.json'),
               latest_experiment_report=str(RUN / 'report_zh.md'), shared_ledger='budget.sqlite'))
    files = sorted(p for p in RUN.rglob('*') if p.is_file() and p.name != 'checksums.json')
    write_json(RUN / 'checksums.json', {str(p.relative_to(RUN)): file_hash(p) for p in files})
    print(json.dumps({'usage': total, 'quality': review['summary'], 'cumulative': cumulative}, ensure_ascii=False))


if __name__ == '__main__':
    main()
