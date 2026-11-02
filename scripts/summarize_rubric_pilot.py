"""Local-only accounting and source-audit summary for the frozen ten-task pilot."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/rubric_pilot10_20261024'

def read(path):
    return json.loads(path.read_text())

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    manifest = read(RUN / 'manifest.json')
    with sqlite3.connect(RUN / 'budget.sqlite') as db:
        db.row_factory = sqlite3.Row
        requests = [dict(row) for row in db.execute('SELECT * FROM requests ORDER BY id')]
    assert not any(row['status'] == 'in_flight' for row in requests), 'Wait for all calls first'
    for task in manifest['tasks']:
        assert sha(RUN / task['input_ref']) == task['input_sha256']
    stats, reviews, by_version = {}, {}, {}
    for version in manifest['authorized_versions']:
        folder = RUN / version
        if not (folder / 'validation.jsonl').exists():
            continue
        records = [json.loads(line) for line in (folder / 'validation.jsonl').read_text().splitlines()]
        review = read(folder / 'source_review.json')
        assert {r['task_id'] for r in records} == {t['task_id'] for t in manifest['tasks']}
        assert {r['task_id'] for r in review['tasks']} == {t['task_id'] for t in manifest['tasks']}
        reviews[version] = review
        usage, models, responses = Counter(), Counter(), 0
        for task in manifest['tasks']:
            response_path = folder / task['task_id'].replace(':', '__') / 'response.json'
            if response_path.exists():
                response = read(response_path)
                responses += 1
                models[response.get('model')] += 1
                usage.update({k: v for k, v in response.get('usage', {}).items() if isinstance(v, int)})
        by_version[version] = dict(successful_responses=responses, usage=dict(usage), response_models=dict(models))
        stats[version] = dict(responses=responses, valid_json=sum(r['items'] > 0 for r in records),
            structure_pass=sum(not r['errors'] for r in records),
            accepted_draft_tasks=sum(r['status'] == 'draft_review_pass_with_notes' for r in review['tasks']),
            items=sum(r['items'] for r in records), total_tokens=usage['total_tokens'],
            method='source_feedback_repair' if version in manifest.get('repair_from', {}) else 'fresh_generation')
    known = sum(json.loads(row['usage'])['total_tokens'] for row in requests if row['usage'])
    unknown = [dict(request_key=row['request_key'], status=row['status'], reservation_tokens=row['reserved']) for row in requests if not row['usage']]
    reserved = sum(x['reservation_tokens'] for x in unknown)
    assert known == sum(v['usage']['total_tokens'] for v in by_version.values()), 'Response/ledger mismatch'
    assert known + reserved == sum(row['reserved'] for row in requests)
    assert known + reserved <= manifest['budget']['max_total_tokens']
    for filename in ('cost_report.json', 'status.json'):
        backup = RUN / filename.replace('.json', '_through_v2.json')
        if not backup.exists():
            backup.write_bytes((RUN / filename).read_bytes())
    cost = dict(by_version=by_version, currency_cost=None, price_verified=False,
        physical_request_attempts=len(requests), known_usage_requests=len(requests)-len(unknown),
        known_total_tokens=known, unknown_usage_attempts=unknown, reserved_unknown_tokens=reserved,
        tokens_accounted_or_reserved=known+reserved, max_total_tokens=800000,
        remaining_accounted_budget=800000-known-reserved)
    write(RUN / 'cost_report.json', cost)
    latest = list(stats)[-1]
    gate = stats[latest]['accepted_draft_tasks'] == 10 and stats[latest]['structure_pass'] == 10
    status = dict(status='bounded_pilot_complete', prepared_tasks=10, rounds=stats,
        latest_tested_prompt=latest, latest_method=stats[latest]['method'],
        latest_source_draft_gate='pass' if gate else 'fail',
        full_run='not_started_explicit_user_instruction', runtime_cases_executed=0,
        held_out_tasks=0, reviewer='Codex source audit; no independent reviewer',
        budget=cost, report='continuation_report_zh.md', stop_reason='final_planned_round_complete; remaining budget insufficient for another complete ten-task round at observed usage')
    if (RUN / 'next_candidate.json').exists():
        status['next_candidate'] = read(RUN / 'next_candidate.json')
    write(RUN / 'status.json', status)
    lines = ['# 同10题 rubric 续跑：v3 至 ' + latest, '',
        '未执行全量。以下是源文对照与纸面反例审查，不是运行检查器得到的准确率；没有执行真实任务或正负轨迹。', '',
        '## 结果', '',
        '| 版本 | 方法 | 返回数 | 合法JSON | 结构及原文引用通过 | 草案暂可保留 | 报告tokens |',
        '|---|---|---:|---:|---:|---:|---:|']
    for v, s in stats.items():
        method = '原始输入独立生成' if s['method'] == 'fresh_generation' else '旧稿＋逐题审查反馈修订'
        lines.append(f"| {v} | {method} | {s['responses']}/10 | {s['valid_json']}/10 | {s['structure_pass']}/10 | {s['accepted_draft_tasks']}/10 | {s['total_tokens']:,} |")
    lines += ['', '“草案暂可保留”表示本轮审查未发现阻断问题，可带明确未决项进入下一步；不是已认证可自动判分。任何一条无依据硬要求、漏项、错误对象绑定或相互矛盾的判定，都阻止该题草案通过。合法JSON与原文引用是脚本检查，其余是Codex逐题静态审查。审查会漏错，计数不能当作客观泛化准确率。', '',
        'v3逐字使用先前的第三版候选prompt；v4改为较短约束。v5起输入增加旧稿和逐题审查反馈，v6/v7进一步强化实际修复、同对象绑定与三态互斥。因此后几轮改善不能归因于单独prompt，也不是新题泛化验证。所有轮次都使用原来的10题。', '',
        '审查中也修正了先前反馈：公开update_task对label有明确字段语义，不能因为另有task_tags就要求额外人工决策；同商品不应无依据升级为同卖方报价。历史记录保留，v5审查注明修正。这也说明审查意见需回到原始来源核实。', '',
        '## 最新一轮逐题结论', '', '| 题目 | 静态审查 | 阻断问题 |', '|---|---|---|']
    for task in reviews[latest]['tasks']:
        findings = '；'.join(f"{f['item_id']}：{f['finding']}" for f in task['findings']) or '未发现阻断问题；仍须读取未决项并做运行验证。'
        findings = findings.replace('|', '\\|').replace('\n', '<br>')
        label = '暂可保留草案' if task['status'] == 'draft_review_pass_with_notes' else '需修订'
        lines.append(f"| {task['task_id']} | {label} | {findings} |")
    lines += ['', '## 已观察到的改进与限制', '',
        '- 交易汇总增加完整集合S及空集0/0规则，不能只计算返回的第一页。',
        '- 批量任务按修改前的完整目标集合检查，避免改为blocked后再筛in_progress导致漏评。',
        '- “1,000-word character limit”保留单位冲突；未消解前判未知，不擅自等同于1000字符。',
        '- 订阅条件要求核实整个45天窗口的重新激活历史或取消机制；不能只看最后的paused状态。',
        '- 上述变化目前只在生成文本和纸面反例中检查，未实现并执行每题检查器。', '',
        '## 预算与执行边界', '',
        f"累计 {len(requests)} 次请求尝试，其中 {len(requests)-len(unknown)} 次有用量返回。已确认 {known:,} tokens；两次早期中断用量未知，仍按原预留合计 {reserved:,} tokens占用预算。累计占用 **{known+reserved:,} / 800,000**，剩余 {800000-known-reserved:,}。没有把未知请求当作零成本，没有按轮重置预算。", '',
        '每个请求先按输入UTF-8字节数、输出上限及开销做保守原子预留，再用返回usage结算；未知用量保留预留。v7改为单并发、大输入先跑，以降低临近预算末尾的同时预留压力，任务身份和内容未改变。金额价格未核验，因此只报告tokens。', '',
        'DeepSeek模型、温度及输出上限保持不变；没有发送真实数据库行、任务执行轨迹、官方Judge答案或密钥文本。v5以后额外发送的只有这些生成稿和基于公开输入的审查反馈。', '',
        '## 产物与下一步', '',
        f'- [最新10题完整五字段rubric]({RUN / latest / "rubrics_zh.md"})',
        f'- [最新prompt]({ROOT / "configs/rubric" / ("generate_" + latest + ".txt")})',
        f'- [最新逐题审查与纸面反例]({RUN / latest / "source_review.json"})',
        f'- [累计用量]({RUN / "cost_report.json"})',
        f'- [原v1/v2历史报告]({RUN / "report_zh.md"})', '',
        '建议保留五字段格式，但暂不全量。下一步先修复最新阻断项，再把完整性、同对象绑定、通过/失败/未知互斥做成检查；用未参与调整的新题及真正的正负执行证据验证。此处只提出后续步骤，本轮没有新增题目或派发全量。', '']
    if (RUN / 'next_candidate.json').exists():
        candidate = read(RUN / 'next_candidate.json')
        lines += [f"已准备[下一版候选prompt]({ROOT / candidate['path']})，针对量词冲突和可选参数的有效值分支补充约束，**未试跑，不计入结果**。剩余额度不足以按近期用量完成另一轮10题，因此没有继续付费请求。", '']
    (RUN / 'continuation_report_zh.md').write_text('\n'.join(lines))
    paths = [ROOT/'scripts/rubric_pilot.py', ROOT/'scripts/summarize_rubric_pilot.py', RUN/'manifest.json', RUN/'cost_report.json', RUN/'status.json', RUN/'continuation_report_zh.md']
    for v in stats:
        paths += [p for p in (RUN/v).rglob('*') if p.is_file() and p.name in ('response.json','prompt.txt','source_review.json','validation.jsonl','repair_provenance.json')]
    if (RUN / 'next_candidate.json').exists():
        paths += [RUN / 'next_candidate.json', ROOT / read(RUN / 'next_candidate.json')['path']]
    write(RUN/'continuation_checksums.json', {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)})
    print(json.dumps(dict(rounds=stats,known_tokens=known,unknown_reserved=reserved,total=known+reserved,remaining=800000-known-reserved),ensure_ascii=False))

if __name__ == '__main__':
    main()
