"""Reconcile frozen requests, usage and source review offline; no model calls."""
import json
import sqlite3
from rubric_qwen_v10 import RUN, PROMPT, read_json, write_json, file_hash, validate_contract
from rubric_qwen_pilot import LEDGER, identity
from rubric_pilot import validate_output


def main():
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    review = read_json(RUN / 'initial/source_review.json')
    by_id = {r['task_id']: r for r in review['tasks']}
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    assert m['prompt_sha256'] == file_hash(PROMPT)
    assert m['review_reference_sha256'] == file_hash(LEDGER.parent / 'review_reference.json')
    with sqlite3.connect(f'file:{LEDGER}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        ledger = [dict(r) for r in db.execute('select * from requests')]
    own = [r for r in ledger if r['request_key'].startswith(m['request_prefix']+'/')]
    assert len(own) == 10 and all(r['status'] == 'response' for r in own)
    rows = []
    for t in m['tasks']:
        d = RUN / 'initial' / t['task_id'].replace(':', '__')
        req = read_json(d / 'request.json')
        assert req == read_json(d / 'planned_request.json')
        assert file_hash(d / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert json.loads(req['messages'][1]['content']) == read_json(t['input_ref'])
        assert req['messages'][0]['content'] == PROMPT.read_text()
        rid = read_json(d / 'request_identity.json')
        assert identity(req, m['api'], t['input_sha256']) == t['signature'] == rid['signature']
        records = [r for r in own if r['request_key'] == rid['request_key']]
        assert len(records) == 1
        response = read_json(d / 'response.json')
        usage = response['usage']
        assert usage == json.loads(records[0]['usage'])
        assert usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens'] == records[0]['reserved']
        assert usage['total_tokens'] < t['reservation_upper'] < 128000
        raw = response['choices'][0]['message']['content']
        try:
            out = json.loads(raw)
            errors = validate_contract(out, read_json(t['input_ref']))
            structure_errors = validate_output(out, read_json(t['input_ref']))
        except (ValueError, TypeError):
            errors = ['invalid_json']
            structure_errors = ['invalid_json']
        if response['choices'][0]['finish_reason'] != 'stop':
            errors.append('incomplete_generation')
            structure_errors.append('incomplete_generation')
        cost = (usage['prompt_tokens']*m['price']['input_per_million'] + usage['completion_tokens']*m['price']['output_per_million'])/1e6
        rows.append(dict(task_id=t['task_id'], **{k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
            estimated_cny=cost, errors=errors, structure_errors=structure_errors, finish_reason=response['choices'][0]['finish_reason'],
            review_status=by_id[t['task_id']]['status'],
            accepted=not errors and by_id[t['task_id']]['status']=='draft_review_pass_with_notes'))
    totals = {k: sum(r[k] for r in rows) for k in ('prompt_tokens','completion_tokens','total_tokens','estimated_cny')}
    assert totals['total_tokens'] <= m['experiment_cap']
    summary = dict(tasks=10, complete_json=sum('invalid_json' not in r['errors'] and r['finish_reason']=='stop' for r in rows),
        structure_and_quote_pass=sum(not r['structure_errors'] for r in rows),
        strict_contract_pass=sum(not r['errors'] for r in rows),
        semantic_review_pass=sum(r['review_status']=='draft_review_pass_with_notes' for r in rows),
        accepted=sum(r['accepted'] for r in rows), runtime_accuracy_measured=False)
    report = dict(initial=totals, summary=summary, physical_requests=10, automatic_retries=0, paid_repairs=0,
        max_actual_input_tokens=max(r['prompt_tokens'] for r in rows),
        max_conservative_request_reservation=max(t['reservation_upper'] for t in m['tasks']),
        price=m['price'], actual_bill_verified=False, codex_review_usage_included=False,
        experiment_cap=m['experiment_cap'], unused_tokens=m['experiment_cap']-totals['total_tokens'],
        cumulative_confirmed_tokens=sum(r['reserved'] for r in ledger if r['status']=='response'),
        historical_unknown_reserved_tokens=sum(r['reserved'] for r in ledger if r['status']!='response'), tasks=rows)
    write_json(RUN / 'cost_report.json', report)
    write_json(RUN / 'status.json', dict(state='completed', full_run=False, summary=summary))
    lines = ['# Qwen3.5-Flash v10：10题试跑', '',
        f"10题各生成一次，完整JSON {summary['complete_json']}/10，结构/原文引用 {summary['structure_and_quote_pass']}/10，严格输出契约 {summary['strict_contract_pass']}/10，静态语义暂可保留 {summary['semantic_review_pass']}/10，二者同时通过 {summary['accepted']}/10。", '',
        '本版仍未达到可靠生成标准；全部10题的criterion均显式包含G/P，但G中混入成功条件、额外硬约束及语义歧义丢失仍普遍存在。5/10固定格式通过不是50%业务准确率，纯格式空格差异也单独列为契约错误，不当作语义错误。', '',
        '## Prompt修改与试验范围', '',
        '新增完整JSON示例、根对象/条目类型约束、全题coverage要求，并要求每条criterion明确写出要求、证据前提G和业务谓词P，三种判定引用同一G/P。保留完整原始来源，禁止从接口example推断真实ID/默认值，强化同一对象、修改前全集、有效默认行为、未来条件和连续引文。', '',
        '采用历史同10题，各单独请求，从零生成，不提供旧稿和逐题反馈。JSON模式，temperature=0，本试跑显式关闭思考，最多8000输出tokens；不改全局Qwen思考配置。自行设置本轮20万token保护上限（不是用户指定总预算）。无收费修复、无重试。', '',
        '## 用量', '',
        '| 输入tokens | 输出tokens | 合计tokens | 估算人民币 |',
        '|---:|---:|---:|---:|',
        f"| {totals['prompt_tokens']:,} | {totals['completion_tokens']:,} | {totals['total_tokens']:,} | ¥{totals['estimated_cny']:.5f} |", '',
        f"每题平均 {totals['total_tokens']/10:,.1f} tokens，¥{totals['estimated_cny']/10:.5f}。最大单次实际输入 {report['max_actual_input_tokens']:,} tokens；发送前UTF-8字节保守上界加输出/余量最多 {report['max_conservative_request_reservation']:,}，低于128,000。完整来源未截断。", '',
        '费用按北京普通API、输入≤128k的输入¥0.2/百万、输出¥2/百万估算（2026-10-24核对[官方价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)）。未核验真实账单，未计缓存/免费额度，不含Codex编程审查用量。', '',
        f"共享历史账本已确认 {report['cumulative_confirmed_tokens']:,} tokens，历史未知预留 {report['historical_unknown_reserved_tokens']:,}；这些历史成本未混入本轮成本。", '',
        '## 逐题结果', '',
        '| 题目 | 输入 | 输出 | 合计 | 格式/引用/固定判定 | 静态语义 |',
        '|---|---:|---:|---:|---|---|']
    for r in rows:
        lines.append(f"| {r['task_id']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | {r['total_tokens']:,} | {'通过' if not r['errors'] else '未通过'} | {'暂可保留' if r['review_status']=='draft_review_pass_with_notes' else '需修改'} |")
    lines += ['', '## 具体审查', '']
    for r in rows:
        checked = by_id[r['task_id']]
        lines += ['### '+r['task_id'], '', checked['notes'], '',
                  '自动检查：'+('通过' if not r['errors'] else '；'.join(r['errors'])), '']
        for f in checked.get('findings', []):
            lines += ['- '+f['item_id']+'：'+f['issue'], '  纸面反例（未执行）：'+f['counterexample']]
        lines += ['']
    lines += ['## 使用边界', '',
        '离线校验器测试通过：1个正确完整示例，加10种错误产物（错题ID、伪造引用、缺失或空G/P、错误判定、字符串条目、多余根键、空coverage、空unresolved、无依据quality）均按预期处理。测试不证明自然语言判定正确。', '',
        '人工静态审查逐题对照冻结义务清单和公开来源；未执行真实任务或正反例，因此不是运行准确率。固定模板通过也不代表G/P语义正确。所有输出保持模型原文，未补写或清洗后计为成功。试验使用已知10题，不能推断新题泛化。若未达到10题格式及语义同时通过，不推广全量。', '',
        f'[新prompt]({PROMPT}) · [完整10题输出]({RUN}/initial/rubrics_zh.md) · [逐题审查]({RUN}/initial/source_review.json) · [用量明细]({RUN}/cost_report.json)', '',
        '离线复核：`agent-world-model/.venv/bin/python scripts/rubric_qwen_v10.py validate`，再运行 `agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v10.py`。', '']
    (RUN / 'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps(dict(total=totals, summary=summary), ensure_ascii=False))


if __name__ == '__main__':
    main()
