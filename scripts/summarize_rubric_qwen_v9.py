"""Offline reconciliation of the fresh Qwen v9 pilot; never calls a model."""
import json
import sqlite3

from rubric_pilot import file_hash, read_json, write_json
from rubric_qwen_pilot import LEDGER, identity
from rubric_qwen_v9 import PROMPT, RUN, SOURCE


def main():
    m = read_json(RUN / 'manifest.json')
    auth = read_json(RUN / 'authorization.json')
    review = read_json(RUN / 'initial/source_review.json')
    checked = {r['task_id']: r for r in review['tasks']}
    validation = {r['task_id']: r for r in map(json.loads, (RUN / 'initial/validation.jsonl').read_text().splitlines())}
    contract = {r['task_id']: r for r in read_json(RUN / 'initial/prompt_contract_validation.json')}
    assert auth['manifest_sha256'] == file_hash(RUN / 'manifest.json')
    assert m['prompt_sha256'] == file_hash(PROMPT)
    assert m['review_reference_sha256'] == file_hash(LEDGER.parent / 'review_reference.json')
    with sqlite3.connect(f'file:{LEDGER}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        ledger = [dict(r) for r in db.execute('select * from requests')]
    own = [r for r in ledger if r['request_key'].startswith(m['request_prefix'] + '/')]
    assert len(own) == 10 and all(r['status'] == 'response' for r in own)
    rows = []
    for t in m['tasks']:
        d = RUN / 'initial' / t['task_id'].replace(':', '__')
        request = read_json(d / 'request.json')
        assert request == read_json(d / 'planned_request.json')
        assert file_hash(d / 'planned_request.json') == t['request_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
        assert json.loads(request['messages'][1]['content']) == read_json(t['input_ref'])
        assert request['messages'][0]['content'] == (RUN / 'initial/prompt.txt').read_text()
        sig = identity(request, m['api'], t['input_sha256'])
        rid = read_json(d / 'request_identity.json')
        assert sig == t['signature'] == rid['signature']
        records = [r for r in own if r['request_key'] == rid['request_key']]
        assert len(records) == 1
        response = read_json(d / 'response.json')
        usage = response['usage']
        assert usage == json.loads(records[0]['usage'])
        assert usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'] == records[0]['reserved']
        assert usage['prompt_tokens'] + usage['completion_tokens'] < t['reservation_upper'] < 128000
        cost = (usage['prompt_tokens'] * m['price']['input_per_million'] +
                usage['completion_tokens'] * m['price']['output_per_million']) / 1_000_000
        rows.append(dict(task_id=t['task_id'], **{k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
            estimated_cny=cost, returned_model=response.get('model'), finish_reason=response['choices'][0]['finish_reason'],
            structure_errors=validation[t['task_id']]['errors'], contract_errors=contract[t['task_id']]['errors'],
            status=checked[t['task_id']]['status']))
    total = {k: sum(r[k] for r in rows) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'estimated_cny')}
    confirmed = sum(r['reserved'] for r in ledger if r['status'] == 'response')
    unknown = sum(r['reserved'] for r in ledger if r['status'] != 'response')
    assert total['total_tokens'] <= auth['experiment_cap']
    prior = read_json(SOURCE / 'cost_report.json')['initial']
    accepted = sum(r['status'] == 'draft_review_pass_with_notes' for r in rows)
    summary = dict(tasks=10, complete_json=sum(r['finish_reason'] == 'stop' for r in rows),
        structure_and_quote_pass=sum(not r['structure_errors'] for r in rows),
        prompt_contract_pass=sum(not r['contract_errors'] for r in rows),
        source_semantic_draft_pass=accepted, remaining_review=10-accepted,
        runtime_semantic_accuracy_measured=False)
    recovery = m.get('network_recovery', {})
    report = dict(initial=total, physical_requests=10, automatic_retries=0, paid_repairs=0,
        transport_aborted_attempts=recovery.get('prior_attempts', 0),
        transport_unknown_reserved_tokens=recovery.get('reserved_unknown_tokens', 0),
        all_attempts_this_turn=10+recovery.get('prior_attempts', 0),
        experiment_cap=m['experiment_cap'], cap_provenance=m['cap_provenance'],
        unused_tokens=m['experiment_cap']-total['total_tokens'],
        cumulative_confirmed_tokens=confirmed, historical_unknown_reserved_tokens=unknown,
        cumulative_accounted_tokens=confirmed+unknown, cumulative_cap=m['cumulative_cap'],
        max_actual_input_tokens=max(r['prompt_tokens'] for r in rows),
        max_conservative_request_reservation=max(t['reservation_upper'] for t in m['tasks']),
        previous_qwen_v8=prior, price=m['price'], actual_bill_verified=False,
        codex_review_usage_included=False, tasks=rows, summary=summary)
    write_json(RUN / 'cost_report.json', report)
    write_json(RUN / 'status.json', dict(state='completed', full_run=False, paid_repairs=0, summary=summary))
    lines = ['# Qwen3.5-Flash v9：10题从零生成试跑', '',
        f"10题各调用一次，总用量 {total['total_tokens']:,} tokens，估算 ¥{total['estimated_cny']:.5f}。",
        f"结构与原文引用通过 {summary['structure_and_quote_pass']}/10；固定G/P判定格式通过 {summary['prompt_contract_pass']}/10；来源语义静态审查暂可保留 {accepted}/10。", '',
        '## 本次修改', '',
        '- 从原题和完整公开来源独立生成，不输入旧rubric或逐题审查反馈；原始sources逐字保留。',
        '- 保留五字段，在criterion内集中定义要求、证据前提G、业务谓词P；三种判定固定引用同一G/P。',
        '- 明确完整证据证明没做应违反；合并同一对象属性，约束存在/全称量词、主外键、完整集合、默认行为、未来条件和连续原文引用。',
        '- 启用JSON输出模式，temperature=0，关闭思考，输出上限8000 tokens。',
        '- 10次请求，每题一次，无重试、无收费修复。额外16万token是本轮自行设置的保护上限，用户授权为10题与单次128k。', '',
        '## 用量与费用', '',
        '| 版本 | 输入tokens | 输出tokens | 合计 | 估算人民币 | 静态审查暂可保留 |',
        '|---|---:|---:|---:|---:|---:|',
        f"| v8旧稿修订 | {prior['prompt_tokens']:,} | {prior['completion_tokens']:,} | {prior['total_tokens']:,} | {prior['estimated_cny']:.5f} | 3/10 |",
        f"| v9从零生成 | {total['prompt_tokens']:,} | {total['completion_tokens']:,} | {total['total_tokens']:,} | {total['estimated_cny']:.5f} | {accepted}/10 |", '',
        f"相较v8，总tokens变化 {(total['total_tokens']/prior['total_tokens']-1)*100:+.2f}%，费用变化 {(total['estimated_cny']/prior['estimated_cny']-1)*100:+.2f}%。平均每题 {total['total_tokens']/10:,.1f} tokens、¥{total['estimated_cny']/10:.5f}。", '',
        '费用按北京区普通API、输入≤128k的输入¥0.2/百万和输出¥2/百万估算；2026-10-24查询[官方价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)。未核对账单，未计免费额度/缓存/Batch折扣；不包含Codex编程和审查用量。', '',
        f"最大实际单次输入 {report['max_actual_input_tokens']:,} tokens；发送前UTF-8字节保守上界加输出预留最大 {report['max_conservative_request_reservation']:,}，均低于128,000。没有截断或删减原始来源来满足上限。", '',
        f"共享账本当前已确认 {confirmed:,}，未知用量预留 {unknown:,}，合计 {confirmed+unknown:,}；有效调用轮次保护上限剩余 {report['unused_tokens']:,}。", '',
        f"网络恢复说明：此前沙箱内有 {report['transport_aborted_attempts']} 次未返回的尝试，DNS探测失败后终止，仍保守预留 {report['transport_unknown_reserved_tokens']:,} tokens，未将其当作零收费。之后在沙箱外发出上述10次请求；总尝试数 {report['all_attempts_this_turn']}，正常返回10次。表中金额仅对应有usage的返回，不是真实总账单。", '',
        '对自动审批提出的数据外发疑问，逐字段核对输入来源，并将本地gen_tasks/gen_spec/gen_db哈希与原始导入清单比较；公开数据集卡及上游README确认全合成来源，证据见public_data_provenance.json。远端文件哈希刷新因网络不可达未完成。补充证据后自动审批通过。输入来自Snowflake/AgentWorldModel-1K公开合成题库，不含实际数据库行、私有轨迹或密钥。', '',
        '## 逐题结果', '',
        '| 题目 | 输入 | 输出 | 合计 | 估算¥ | 格式/引用 | 静态语义审查 |',
        '|---|---:|---:|---:|---:|---|---|']
    for r in rows:
        lines.append(f"| {r['task_id']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | {r['total_tokens']:,} | {r['estimated_cny']:.5f} | {'通过' if not r['structure_errors'] else '未通过'} | {'暂可保留' if r['status']=='draft_review_pass_with_notes' else '需修改'} |")
    lines += ['', '## 来源语义审查', '']
    for r in review['tasks']:
        lines += ['### '+r['task_id'], '', r['notes'], '']
        for f in r.get('findings', []):
            lines += ['- '+f['item_id']+'：'+f['issue'], '  纸面反例（未执行）：'+f['counterexample']]
        lines += ['']
    lines += ['## 验证范围与复核', '',
        '固定三值模板只保证三个分支引用同一组定义，不能保证模型定义的G/P语义正确。以上语义审查逐题对照既有冻结义务清单、原题、接口和schema；不是运行任务或执行正反例所得的准确率。', '',
        '这是在已知10题上的开发试验，同时改了prompt、输入策略、JSON模式和输出上限。可以比较本轮原始产物和成本，但不能将变化单独归因于prompt，也不能推断新题泛化。未修改原始模型输出，未用旧稿回填，未全量生成或接入训练奖励。', '',
        f'[新prompt]({PROMPT}) · [10题完整rubric]({RUN}/initial/rubrics_zh.md) · [逐题审查]({RUN}/initial/source_review.json) · [token账单明细]({RUN}/cost_report.json)', '',
        '离线复核（不调用API）：', '',
        '```bash', 'agent-world-model/.venv/bin/python scripts/rubric_qwen_v9.py validate',
        'agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v9.py', '```', '']
    (RUN / 'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps(dict(total=total, summary=summary), ensure_ascii=False))


if __name__ == '__main__':
    main()
