"""Reconcile v8 request lineage, original-source review, usage and user-priced estimates offline."""
import json
import sqlite3

from rubric_pilot import file_hash, read_json, write_json
from rubric_qwen_pilot import LEDGER, identity
from rubric_qwen_v8 import PROMPT, RUN, SOURCE


def main():
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    review = read_json(RUN / 'initial/source_review.json')
    checked = {t['task_id']: t for t in review['tasks']}
    validation = {t['task_id']: t for t in map(json.loads, (RUN / 'initial/validation.jsonl').read_text().splitlines())}
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    assert file_hash(PROMPT) == m['prompt_sha256']
    assert file_hash(SOURCE / 'initial/source_review.json') == m['source_review_sha256']
    assert file_hash(LEDGER.parent / 'review_reference.json') == m['review_reference_sha256']
    with sqlite3.connect(LEDGER) as db:
        db.row_factory = sqlite3.Row
        ledger = [dict(r) for r in db.execute('select * from requests')]
    old_review = {t['task_id']: t for t in read_json(SOURCE / 'initial/source_review.json')['tasks']}
    rows = []
    for t in m['tasks']:
        key = t['task_id'].replace(':', '__')
        folder = RUN / 'initial' / key
        request = read_json(folder / 'request.json')
        assert request == read_json(folder / 'planned_request.json')
        assert file_hash(folder / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert file_hash(SOURCE / 'initial' / key / 'rubric.json') == t['draft_sha256']
        assert request['messages'][0]['content'] == (RUN / 'initial/prompt.txt').read_text()
        payload = json.loads(request['messages'][1]['content'])
        assert payload['sources'] == read_json(t['input_ref'])['sources']
        assert json.loads(payload['previous_rubric_raw']) == read_json(SOURCE / 'initial' / key / 'rubric.json')
        f = old_review[t['task_id']]
        assert payload['source_review_feedback'] == {k: f[k] for k in ('task_id', 'status', 'findings')}
        sig = identity(request, m['api'], t['input_sha256'])
        rid = read_json(folder / 'request_identity.json')
        assert sig == t['signature'] == rid['signature']
        records = [r for r in ledger if r['request_key'] == rid['request_key']]
        assert len(records) == 1 and records[0]['status'] == 'response'
        response = read_json(folder / 'response.json')
        usage = response['usage']
        assert usage == json.loads(records[0]['usage'])
        assert usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'] == records[0]['reserved']
        assert usage['prompt_tokens'] < 128000
        assert t['reservation_upper'] < 128000
        cost = (usage['prompt_tokens'] * 0.2 + usage['completion_tokens'] * 2) / 1_000_000
        rows.append(dict(task_id=t['task_id'], prompt_tokens=usage['prompt_tokens'],
            completion_tokens=usage['completion_tokens'], total_tokens=usage['total_tokens'],
            estimated_cny=cost, returned_model=response.get('model'), finish_reason=response['choices'][0]['finish_reason'],
            structure_errors=validation[t['task_id']]['errors'], status=checked[t['task_id']]['status']))
    own = [r for r in ledger if r['request_key'].startswith(m['request_prefix'] + '/')]
    assert len(own) == 10 and all(r['status'] == 'response' for r in own)
    assert not any(r['status'] == 'in_flight' for r in ledger)
    total = {k: sum(r[k] for r in rows) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'estimated_cny')}
    confirmed = sum(json.loads(r['usage'])['total_tokens'] for r in ledger if r['status'] == 'response')
    unknown = sum(r['reserved'] for r in ledger if r['status'] != 'response')
    cumulative = confirmed + unknown
    assert cumulative - m['budget_before']['tokens_accounted_or_reserved'] == total['total_tokens'] <= auth['experiment_cap']
    assert cumulative <= m['cumulative_cap']
    prior = read_json(SOURCE / 'cost_report.json')['initial']
    prior_cost = (prior['prompt_tokens'] * 0.2 + prior['completion_tokens'] * 2) / 1_000_000
    cost = dict(model=m['model'], initial=total, physical_requests=10, automatic_retries=0, paid_repairs=0,
        experiment_cap=160000, unused_tokens=160000-total['total_tokens'], cumulative_accounted_tokens=cumulative,
        cumulative_confirmed_tokens=confirmed, historical_unknown_reserved_tokens=unknown, cumulative_cap=m['cumulative_cap'],
        cumulative_physical_requests=len(ledger), max_actual_input_tokens=max(r['prompt_tokens'] for r in rows),
        max_conservative_request_reservation=max(t['reservation_upper'] for t in m['tasks']),
        previous_qwen=prior, previous_qwen_estimated_cny=prior_cost, price_assumption=m['user_supplied_price'],
        actual_bill_verified=False, codex_review_usage_included=False, tasks=rows)
    write_json(RUN / 'cost_report.json', cost)
    summary = review['summary']
    write_json(RUN / 'status.json', dict(state='completed', full_run=False, paid_repairs=0, summary=summary))
    lines = ['# Qwen3.5-Flash v8 prompt：同10题试跑结果', '',
        f"完成10题，实际{total['total_tokens']:,} tokens，平均每题{total['total_tokens']/10:,.1f}；按用户截图普通API价格估算共¥{total['estimated_cny']:.5f}，平均每题¥{total['estimated_cny']/10:.5f}。", '',
        f"按同一原始来源与语义审查，{summary['accepted_draft_tasks']}/10份草案暂可保留；结构与原文引用通过{summary['structure_pass_tasks']}/10。未追加修复、未全量跑。", '',
        f"本轮没有改善总体验收结果：上轮4/10，本轮{summary['accepted_draft_tasks']}/10。分项为：{summary['accepted_draft_tasks']}题综合通过，{summary['quote_only_failed_tasks']}题仅引用未通过，{summary['semantic_blocked_tasks']}题存在语义阻断，{summary['generation_invalid_tasks']}题生成截断无效。语义暂可保留{summary['semantic_provisional_pass_tasks']}题，另有1题因输出无效不作语义判断。", '',
        '## 修改内容', '',
        '保留v7五字段、三种互斥判定、完整集合、同一对象、真实证据和歧义门控要求；增加存在量词、多记录绑定、有效默认参数、缺失动作与缺失证据、本地过滤路径的具体自检。引用优先摘录连续自然语言短句，不拼接JSON结构。', '',
        '恢复完整原始sources（含example但仍禁止把示例当业务规则），旧稿改用上一轮Qwen实际输出，附其逐题来源审查反馈；仅去掉旧稿JSON的格式空白，不删字段。系统prompt、来源、旧稿和反馈均冻结并校验哈希。', '',
        '这是已知10题上的迭代修订，同时改变prompt、恢复来源并更新草稿/反馈；效果不能单独归因于prompt，也不代表新题首次生成的质量。', '',
        '局部变化：不匹配引用由18处减至13处；API后本地过滤路径、已核实缺省增量行为已有补充，但相关题的其他逻辑漏洞仍未解决。订阅题原先暂可保留，本轮达到10,000输出tokens后截断为无效JSON，使综合通过数减少1题。截断请求的全部用量已计入费用，不用旧稿回填成绩。', '',
        '结论：v8保留为实验版本，不推广全量。后续应先把引用选择与校验、缺失证据门控和互补判定做成可单独检查的局部步骤，再验证少量失败项；本轮未追加调用或另跑新版本。', '',
        '## 用量与对照', '',
        '| 轮次 | 输入tokens | 输出tokens | 合计tokens | 综合草案通过 |',
        '|---|---:|---:|---:|---:|',
        '| 原v7 DeepSeek | 85,701 | 24,689 | 110,390 | 8/10 |',
        '| 输入去重后 DeepSeek | 78,468 | 24,484 | 102,952 | 7/10 |',
        '| 上轮 Qwen | 73,183 | 30,449 | 103,632 | 4/10（仅语义6/10） |',
        f"| 本轮 Qwen v8 | {total['prompt_tokens']:,} | {total['completion_tokens']:,} | {total['total_tokens']:,} | {summary['accepted_draft_tasks']}/10 |", '',
        f"价格假设来自用户截图：单次输入≤128k，普通API输入¥0.2/百万、输出¥2/百万。上轮Qwen按同价估算¥{prior_cost:.5f}；本轮¥{total['estimated_cny']:.5f}。未应用Batch折扣、缓存折扣或免费额度，未核验真实账单，Codex审查用量另计。", '',
        f"最大实际单次输入{cost['max_actual_input_tokens']:,} tokens；请求输入的保守字节上界连同输出预留最大{cost['max_conservative_request_reservation']:,}，均小于128,000。本轮16万上限剩余{cost['unused_tokens']:,}。", '',
        f"累计账本：已确认{confirmed:,} + 历史未知预留{unknown:,} = {cumulative:,}，累计上限{m['cumulative_cap']:,}。本轮10次请求均有usage，无额外重试。", '',
        '## 逐题审查', '', '| 题目 | 输入 | 输出 | 总tokens | 格式/原文引用 | 综合草案 |',
        '|---|---:|---:|---:|---|---|']
    for r in rows:
        label = {'draft_review_pass_with_notes': '暂可保留', 'generation_invalid': '生成无效'}.get(r['status'], '需修改')
        lines.append(f"| {r['task_id']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | {r['total_tokens']:,} | {'通过' if not r['structure_errors'] else '不通过'} | {label} |")
    lines += ['', '## 具体发现', '']
    for t in review['tasks']:
        lines += [f"### {t['task_id']}", '', t['review_note'], '']
        for finding in t['findings']:
            lines.append(f"- {finding['item_id']}：{finding['finding']}")
            if finding.get('paper_counterexample'):
                lines.append(f"  纸面反例（未执行）：{finding['paper_counterexample']}")
        lines.append('')
    lines += ['## 验证边界', '',
        '采用与此前相同的冻结题面义务清单、完整原始来源校验和Codex静态语义审查。纸面反例未执行，也未运行真实任务/验收检查器；暂可保留不等于可直接用作训练reward。未用本地补写或历史结果替换本次模型输出。', '',
        f"[新prompt]({PROMPT}) · [10题完整rubric]({RUN / 'initial/rubrics_zh.md'}) · [逐题审查]({RUN / 'initial/source_review.json'}) · [用量和价格假设]({RUN / 'cost_report.json'})", '',
        '离线复核：`agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v8.py`。']
    (RUN / 'report_zh.md').write_text('\n'.join(lines)+'\n')
    write_json(LEDGER.parent / 'cost_followup.json', dict(cap=m['cumulative_cap'], cumulative_accounted_tokens=cumulative,
        historical_reports_frozen=True, latest_cost_report=str(RUN / 'cost_report.json'),
        latest_experiment_report=str(RUN / 'report_zh.md'), shared_ledger='budget.sqlite'))
    write_json(RUN / 'checksums.json', {str(p.relative_to(RUN)): file_hash(p)
        for p in sorted(RUN.rglob('*')) if p.is_file() and p.name != 'checksums.json'})
    print(json.dumps(dict(usage=total, summary=summary, cumulative=cumulative), ensure_ascii=False))


if __name__ == '__main__':
    main()
