"""Offline usage and paired-review report; never dispatches API requests."""
import json
import sqlite3

from rubric_pilot import file_hash, read_json, write_json
from rubric_qwen_pilot import LEDGER, RUN, SOURCE, identity


def main():
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    review = read_json(RUN / 'initial/source_review.json')
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    assert file_hash(LEDGER.parent / 'v7/source_review.json') == m['baseline_review_sha256']
    assert file_hash(LEDGER.parent / 'review_reference.json') == m['review_reference_sha256']
    reviewed = {t['task_id']: t for t in review['tasks']}
    validation = {r['task_id']: r for r in map(json.loads, (RUN / 'initial/validation.jsonl').read_text().splitlines())}
    with sqlite3.connect(LEDGER) as db:
        db.row_factory = sqlite3.Row
        records = [dict(r) for r in db.execute('select * from requests')]
    rows = []
    for t in m['tasks']:
        key = t['task_id'].replace(':', '__')
        d = RUN / 'initial' / key
        req = read_json(d / 'request.json')
        old = read_json(SOURCE / 'initial' / key / 'request.json')
        assert req == read_json(d / 'planned_request.json')
        assert req['messages'] == old['messages']
        assert {k: v for k, v in req.items() if k not in ('model', 'extra_body')} == {
            k: v for k, v in old.items() if k not in ('model', 'extra_body')}
        assert file_hash(d / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        sig = identity(req, m['api'], t['input_sha256'])
        assert sig == t['signature'] == read_json(d / 'request_identity.json')['signature']
        request_key = read_json(d / 'request_identity.json')['request_key']
        ledger_rows = [r for r in records if r['request_key'] == request_key]
        assert len(ledger_rows) == 1 and ledger_rows[0]['status'] == 'response'
        response = read_json(d / 'response.json')
        usage = response['usage']
        assert usage == json.loads(ledger_rows[0]['usage'])
        assert usage['total_tokens'] == ledger_rows[0]['reserved']
        assert usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens']
        detail = usage.get('prompt_tokens_details') or {}
        rows.append(dict(task_id=t['task_id'], prompt_tokens=usage['prompt_tokens'],
            completion_tokens=usage['completion_tokens'], total_tokens=usage['total_tokens'],
            cached_tokens=detail.get('cached_tokens', usage.get('prompt_cache_hit_tokens', 0)) or 0,
            cache_usage_reported='cached_tokens' in detail or 'prompt_cache_hit_tokens' in usage,
            response_model=response.get('model'), finish_reason=response['choices'][0]['finish_reason'],
            structure_errors=validation[t['task_id']]['errors'], status=reviewed[t['task_id']]['status']))
    own = [r for r in records if r['request_key'].startswith('qwen_rubric_initial/')]
    assert len(own) == 10 and all(r['status'] == 'response' for r in own)
    assert not any(r['status'] == 'in_flight' for r in records)
    totals = {k: sum(t[k] for t in rows) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens')}
    confirmed = sum(json.loads(r['usage'])['total_tokens'] for r in records if r['status'] == 'response')
    unknown = sum(r['reserved'] for r in records if r['status'] != 'response')
    cumulative = confirmed + unknown
    before = m['budget_before']['tokens_accounted_or_reserved']
    assert cumulative - before == totals['total_tokens'] <= auth['experiment_cap']
    prior = read_json(SOURCE / 'cost_report.json')['initial']
    cost = dict(model=m['model'], base_url=m['base_url'], initial=totals, physical_requests=10,
        repairs=0, automatic_retries=0, experiment_cap=auth['experiment_cap'],
        unused_tokens=auth['experiment_cap']-totals['total_tokens'], cumulative_confirmed_tokens=confirmed,
        cumulative_unknown_reserved_tokens=unknown, cumulative_accounted_tokens=cumulative,
        cumulative_cap=before+auth['experiment_cap'], cumulative_physical_requests=len(records),
        prior_deepseek=prior, saving_tokens=prior['total_tokens']-totals['total_tokens'],
        saving_percent=100*(1-totals['total_tokens']/prior['total_tokens']),
        currency_cost=None, codex_review_usage_included=False, tasks=rows)
    write_json(RUN / 'cost_report.json', cost)
    summary = review['summary']
    write_json(RUN / 'status.json', dict(state='completed', full_run=False, paid_repairs=0, summary=summary))
    lines = ['# Qwen3.5-Flash 同10题 rubric 试跑', '',
        f"完成10次请求，实际{totals['total_tokens']:,} tokens，平均每题{totals['total_tokens']/10:,.1f}。",
        f"草案来源静态审查暂可保留{summary['accepted_draft_tasks']}/10；结构与引用检查通过{sum(not t['structure_errors'] for t in rows)}/10。未追加修复、未全量跑。", '',
        '## 对照与实际用量', '',
        '本次沿用上一轮完整system与user消息（逐字校验），包含v7约束、同一题面/公开契约/schema、同一v6旧稿与反馈。仅模型及对应的关闭思考参数改变；temperature=0、max_tokens=10000、JSON输出模式不变。', '',
        f"配置的API为{m['base_url']}，请求模型{m['model']}。原始供应商响应的model及usage分别保存。没有额外调用模型进行审查。", '',
        '| 单轮 | 输入tokens | 输出tokens | 总tokens | 静态审查暂可保留 |',
        '|---|---:|---:|---:|---:|',
        '| 原v7 DeepSeek | 85,701 | 24,689 | 110,390 | 8/10 |',
        '| 输入去重后 DeepSeek | 78,468 | 24,484 | 102,952 | 7/10 |',
        f"| 本次 Qwen | {totals['prompt_tokens']:,} | {totals['completion_tokens']:,} | {totals['total_tokens']:,} | {summary['accepted_draft_tasks']}/10 |", '',
        f"比同输入DeepSeek{'减少' if cost['saving_tokens'] >= 0 else '增加'}{abs(cost['saving_tokens']):,} tokens（{abs(cost['saving_percent']):.2f}%）。不同模型的分词方式及生成长度均可能影响计数；不能把这个差值归因于再次压缩输入，也不代表相同比例的人民币费用变化。", '',
        f"本次上限{auth['experiment_cap']:,}，未用{cost['unused_tokens']:,}。累计已确认{confirmed:,} + 历史未知预留{unknown:,} = {cumulative:,}，累计上限{cost['cumulative_cap']:,}。本次无未知用量请求。", '',
        (f"供应商报告的缓存命中{totals['cached_tokens']:,}已包含在输入用量中，不再扣除。" if any(t['cache_usage_reported'] for t in rows) else '本次usage未报告缓存命中详情，不推断缓存命中为零。') + '未查实际账单价格，不报告货币费用；Codex审查tokens未计入。', '',
        '## 质量分项', '',
        f"按原始来源引用规则和语义审查同时合格的草案为{summary['accepted_draft_tasks']}/10。仅看判定语义，{summary['semantic_provisional_pass_tasks']}/10暂可保留；另有{summary['semantic_failure_tasks']}题存在语义阻断。", '',
        f"原始来源引用不兼容涉及{summary['quote_incompatible_tasks']}题、{summary['quote_incompatible_references']}处引用。逐处检查发现这些引文都与实际提供的压缩来源连续匹配，因此不能称为凭空编造：压缩器删除example后改变了JSON邻接关系，而验收要求匹配完整原文。{summary['quote_only_failure_tasks']}题仅因这一兼容问题未通过；不把尚未修复的输出算作已合格。", '',
        '该结果没有达到8/10目标。未自动修复引用以掩盖原始输出问题，也未修改prompt或追加模型请求。下一步应先离线处理压缩来源与原文定位的映射，再单独检验歧义门控、存在量词和缺失动作分支；无需重跑已经保留的草案。', '',
        '## 逐题结果', '', '| 题目 | 输入 | 输出 | 合计 | 结构/引用 | 综合草案审查 |',
        '|---|---:|---:|---:|---|---|']
    for t in rows:
        lines.append(f"| {t['task_id']} | {t['prompt_tokens']:,} | {t['completion_tokens']:,} | {t['total_tokens']:,} | {'通过' if not t['structure_errors'] else '不通过'} | {'暂可保留' if t['status']=='draft_review_pass_with_notes' else '需修改'} |")
    lines += ['', '## 审查发现', '']
    for t in review['tasks']:
        if t['findings']:
            lines += [f"### {t['task_id']}", '']
            for f in t['findings']:
                lines.append(f"- {f['item_id']}：{f['finding']}")
                if f.get('paper_counterexample'):
                    lines.append(f"  纸面反例（未执行）：{f['paper_counterexample']}")
            lines.append('')
    lines += ['## 解释边界', '',
        '质量统计采用同一冻结义务清单、原始来源和逐条语义审查；结构合格不等于语义正确。纸面反例未执行，没有运行真实任务或验收检查器，因此不能把草案比例称为检查器准确率。', '',
        '本次是旧稿修订对照，并非从零生成新题rubric。旧稿和反馈的历史生成成本未包含在本轮用量；此前多轮累计开发用量不能与单轮费用混算。仅10题且已反复审查，不能据此保证一万道新题质量。', '',
        f"[完整五字段rubric]({RUN / 'initial/rubrics_zh.md'}) · [逐题审查JSON]({RUN / 'initial/source_review.json'}) · [用量JSON]({RUN / 'cost_report.json'})", '',
        '离线复核：`agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen.py`。']
    (RUN / 'report_zh.md').write_text('\n'.join(lines)+'\n')
    write_json(LEDGER.parent / 'cost_followup.json', dict(cap=cost['cumulative_cap'], cumulative_accounted_tokens=cumulative,
        historical_reports_frozen=True, latest_cost_report=str(RUN / 'cost_report.json'),
        latest_experiment_report=str(RUN / 'report_zh.md'), shared_ledger='budget.sqlite'))
    write_json(RUN / 'checksums.json', {str(p.relative_to(RUN)): file_hash(p)
        for p in sorted(RUN.rglob('*')) if p.is_file() and p.name != 'checksums.json'})
    print(json.dumps(dict(usage=totals, summary=summary, cumulative=cumulative), ensure_ascii=False))


if __name__ == '__main__':
    main()
