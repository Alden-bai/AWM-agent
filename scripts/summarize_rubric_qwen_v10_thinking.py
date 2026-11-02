"""Offline paired thinking/non-thinking cost and quality reconciliation."""
import json
import sqlite3

from rubric_qwen_v10_thinking import BASE, RUN, PROMPT, paired_request, reservation, baseline
from rubric_qwen_pilot import LEDGER, identity
from rubric_pilot import read_json, write_json, file_hash, validate_output, unique_pairs


def main():
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    review = read_json(RUN / 'initial/source_review.json')
    reviews = {r['task_id']: r for r in review['tasks']}
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    for key, path in [('prompt_sha256', PROMPT), ('baseline_manifest_sha256', BASE/'manifest.json'),
                      ('baseline_cost_report_sha256', BASE/'cost_report.json'),
                      ('validator_sha256', baseline.__file__),
                      ('review_reference_sha256', LEDGER.parent/'review_reference.json')]:
        assert m[key] == file_hash(path), key
    prior = read_json(BASE / 'cost_report.json')
    with sqlite3.connect(f'file:{LEDGER}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        ledger = [dict(r) for r in db.execute('select * from requests')]
    own = [r for r in ledger if r['request_key'].startswith(m['request_prefix']+'/')]
    assert len(own) == 10 and all(r['status'] == 'response' for r in own)
    rows = []
    for t in m['tasks']:
        d = RUN / 'initial' / t['task_id'].replace(':','__')
        request = read_json(d/'request.json')
        old = BASE / 'initial' / d.name / 'request.json'
        assert file_hash(old) == t['baseline_request_sha256']
        assert request == paired_request(read_json(old)) == read_json(d/'planned_request.json')
        assert file_hash(d/'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert request['messages'][0]['content'] == PROMPT.read_text()
        assert identity(request, m['api'], t['input_sha256']) == t['signature']
        rid = read_json(d/'request_identity.json')
        assert rid['signature'] == t['signature']
        records = [r for r in own if r['request_key'] == rid['request_key']]
        assert len(records) == 1
        resp = read_json(d/'response.json')
        choice = resp['choices'][0]
        usage = resp['usage']
        assert usage == json.loads(records[0]['usage'])
        assert usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens'] == records[0]['reserved']
        assert usage['prompt_tokens'] < 128000
        assert usage['total_tokens'] < reservation(request) == t['reservation_upper'] < 128000
        reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
        if reasoning is not None:
            assert 0 <= reasoning <= usage['completion_tokens']
        inp = read_json(t['input_ref'])
        raw = choice['message'].get('content') or ''
        try:
            out = json.loads(raw, object_pairs_hook=unique_pairs)
            structure = validate_output(out, inp)
            strict = baseline.validate_contract(out, inp)
            complete = choice['finish_reason'] == 'stop'
        except (ValueError, TypeError):
            structure = strict = ['invalid_json']
            complete = False
        if choice['finish_reason'] != 'stop':
            structure = structure + ['incomplete_generation']
            strict = strict + ['incomplete_generation']
        status = reviews[t['task_id']]['status']
        assert complete or status == 'invalid_generation'
        cost = (usage['prompt_tokens']*m['price']['input_per_million'] + usage['completion_tokens']*m['price']['output_per_million'])/1e6
        rows.append(dict(task_id=t['task_id'], **{k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')},
            reasoning_tokens=reasoning, answer_tokens=usage['completion_tokens']-reasoning if reasoning is not None else None,
            reasoning_present=bool(choice['message'].get('reasoning_content')), returned_model=resp.get('model'),
            finish_reason=choice['finish_reason'], complete_json=complete, estimated_cny=cost,
            structure_errors=structure, errors=strict, review_status=status,
            accepted=complete and not strict and status=='draft_review_pass_with_notes'))
    totals = {k:sum(r[k] for r in rows) for k in ('prompt_tokens','completion_tokens','total_tokens','estimated_cny')}
    split_known = all(r['reasoning_tokens'] is not None for r in rows)
    totals.update(reasoning_tokens=sum(r['reasoning_tokens'] for r in rows) if split_known else None,
                  answer_tokens=sum(r['answer_tokens'] for r in rows) if split_known else None)
    assert totals['total_tokens'] <= m['experiment_cap']
    summary = dict(tasks=10, complete_json=sum(r['complete_json'] for r in rows),
        reasoning_responses=sum(r['reasoning_present'] for r in rows),
        reasoning_budget_hit=sum(r['reasoning_tokens'] == m['thinking_budget'] for r in rows),
        structure_and_quote_pass=sum(not r['structure_errors'] for r in rows),
        strict_contract_pass=sum(not r['errors'] for r in rows),
        semantic_review_pass=sum(r['review_status']=='draft_review_pass_with_notes' for r in rows),
        accepted=sum(r['accepted'] for r in rows), runtime_accuracy_measured=False)
    comparison = {k:dict(baseline=prior['initial'][k], thinking=totals[k],
        delta=totals[k]-prior['initial'][k], percent=(totals[k]/prior['initial'][k]-1)*100)
        for k in ('prompt_tokens','completion_tokens','total_tokens','estimated_cny')}
    report = dict(initial=totals, baseline=prior['initial'], baseline_summary=prior['summary'], comparison=comparison,
        summary=summary, messages_identical=True, thinking_budget=m['thinking_budget'], max_answer_tokens=8000,
        physical_requests=10, automatic_retries=0, paid_repairs=0, actual_bill_verified=False,
        codex_review_usage_included=False, price=m['price'], experiment_cap=m['experiment_cap'],
        max_actual_input_tokens=max(r['prompt_tokens'] for r in rows),
        max_conservative_reservation=max(t['reservation_upper'] for t in m['tasks']),
        cumulative_confirmed_tokens=sum(r['reserved'] for r in ledger if r['status']=='response'),
        historical_unknown_reserved_tokens=sum(r['reserved'] for r in ledger if r['status']!='response'),tasks=rows)
    write_json(RUN/'cost_report.json', report)
    write_json(RUN/'status.json',dict(state='completed',full_run=False,summary=summary))
    lines=['# v10 同题同prompt：思考模式对照', '',
        f"开启思考后，静态语义暂可保留 {summary['semantic_review_pass']}/10（原0/10）；完整JSON {summary['complete_json']}/10，结构/引用 {summary['structure_and_quote_pass']}/10，严格输出契约 {summary['strict_contract_pass']}/10，全部同时通过 {summary['accepted']}/10。", '',
        f"本轮费用为非思考版的 {totals['estimated_cny']/prior['initial']['estimated_cny']:.2f} 倍，总tokens增加 {comparison['total_tokens']['percent']:.2f}%。静态语义通过数未提升，暂不能证明开启思考改善了本任务的可用质量。", '',
        '## 控制变量', '',
        '逐题核对请求哈希及messages：系统prompt、用户输入、来源、题目集合、模型、temperature=0、response_format=json_object、最终回答max_tokens=8000保持一致。仅启用enable_thinking=true，并设置thinking_budget=16384。客户端timeout从240秒增到600秒，只影响等待。未提供旧稿或审查反馈。每题各一次，无重试或收费修复。', '',
        f"有 {summary['reasoning_budget_hit']}/10 题思考token达到16,384上限；这对结论范围有影响，不能据此断言更大思考预算也无效。", '',
        '思考上限是本轮预算控制参数；因此这是该上限下的模式对照，不是无限思考结果。20万的旧轮次保护上限与本轮自行设置的30万上限都不是用户指定总预算。', '',
        '最多两个请求并发，共用同一request_key及预算账本。原派发器遇到另一派发器已认领的在途任务时，在发送前停止；这不是API失败或重试。最终逐条核对10条response及usage。', '',
        '相同messages在两种模式下服务端输入token计数可能不同；这里采用各次返回usage，未删减来源来人为降低输入。', '',
        '## Token及估算费用', '',
        '| 指标 | 非思考 | 思考 | 变化 |','|---|---:|---:|---:|']
    for k,label in [('prompt_tokens','输入tokens'),('completion_tokens','输出tokens（含思考）'),('total_tokens','合计tokens'),('estimated_cny','估算人民币')]:
        c=comparison[k]
        a=f"¥{c['baseline']:.6f}" if k=='estimated_cny' else f"{c['baseline']:,}"
        b=f"¥{c['thinking']:.6f}" if k=='estimated_cny' else f"{c['thinking']:,}"
        lines.append(f"| {label} | {a} | {b} | {c['percent']:+.2f}% |")
    lines += ['', f"每题平均 {totals['total_tokens']/10:,.1f} tokens，¥{totals['estimated_cny']/10:.6f}。最大实际输入 {report['max_actual_input_tokens']:,} tokens；保守输入字节上界加思考/回答/余量最大 {report['max_conservative_reservation']:,}，低于128,000。", '',
        f"API返回含reasoning_content的响应为 {summary['reasoning_responses']}/10。" +
        (f"服务端usage单列思考 {totals['reasoning_tokens']:,}、最终回答 {totals['answer_tokens']:,} tokens；均已包含在输出总量，不再重复计费。" if split_known else '服务端未完整提供reasoning_tokens拆分，因此不猜测思考/最终回答各自token数；总输出usage已全部计费。'), '',
        '按[官方Qwen3.5-Flash北京普通API价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)：输入≤128k时输入¥0.2/百万、输出¥2/百万。[思考内容按输出计费](https://help.aliyun.com/zh/model-studio/deep-thinking)。金额为未计缓存/免费额度的估算，未核对真实账单，不含Codex审查费用。', '',
        f"共享账本已确认 {report['cumulative_confirmed_tokens']:,} tokens，历史未知预留 {report['historical_unknown_reserved_tokens']:,}；本轮10次均有usage，历史费用未混入本轮。", '',
        '## 质量对照', '', '| 指标 | 非思考 | 思考 |','|---|---:|---:|']
    for k,label in [('complete_json','完整JSON'),('structure_and_quote_pass','结构与原文引用'),('strict_contract_pass','严格输出契约'),('semantic_review_pass','静态语义暂可保留'),('accepted','同时通过')]:
        lines.append(f"| {label} | {prior['summary'][k]}/10 | {summary[k]}/10 |")
    lines += ['', '## 逐题', '', '| 题目 | 输入 | 输出含思考 | 思考子项 | 估算¥ | 格式/引用 | 静态语义 |','|---|---:|---:|---:|---:|---|---|']
    for r in rows:
        lines.append(f"| {r['task_id']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | {r['reasoning_tokens'] if r['reasoning_tokens'] is not None else '未返回'} | {r['estimated_cny']:.6f} | {'通过' if not r['errors'] else '未通过'} | {'暂可保留' if r['review_status']=='draft_review_pass_with_notes' else r['review_status']} |")
    lines += ['', '## 静态来源审查', '']
    for r in rows:
        reviewed=reviews[r['task_id']]
        lines += ['### '+r['task_id'], '', reviewed['notes'], '', '自动检查：'+('通过' if not r['errors'] else '；'.join(r['errors'])), '']
        for f in reviewed.get('findings',[]):
            lines += ['- '+f['item_id']+'：'+f['issue'], '  纸面反例（未执行）：'+f['counterexample']]
        lines += ['']
    lines += ['## 验证边界', '',
        '沿用相同校验器、冻结题面义务清单及原始来源；每题审查G/P、范围、量词、关联、时间、完整交付和合法实现路径。纯格式问题与语义问题分开。原始回答不补写、不换旧稿；截断算生成无效。未执行真实任务或纸面反例，所以不是运行准确率；单次10题不能建立统计显著性或新题泛化结论。', '',
        '离线测试核验了思考预算在请求前被预留、超预算拒绝、实际usage结算，以及10份请求除思考参数外完全相同。', '',
        f'[同一prompt]({PROMPT}) · [思考版10题完整rubric]({RUN}/initial/rubrics_zh.md) · [费用明细]({RUN}/cost_report.json) · [来源审查]({RUN}/initial/source_review.json)', '',
        '离线复核：`agent-world-model/.venv/bin/python scripts/rubric_qwen_v10_thinking.py validate`；`agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v10_thinking.py`。', '']
    (RUN/'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps(dict(total=totals,summary=summary,comparison=comparison),ensure_ascii=False))


if __name__ == '__main__': main()
