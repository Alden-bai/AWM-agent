"""Offline report: distinguish model simulation from independent source review."""
import json

from rubric_qwen_v11 import RUN, BASE, ROOT, summarize, parse_response, STAGES
from rubric_pilot import read_json, write_json, file_hash
from rubric_qwen_v10 import validate_contract
from rubric_workflow import normalize, generator_input


def verify_artifacts(manifest):
    """Reconstruct every stage input; review reference/cases cannot leak silently."""
    for stage, expected_hash in manifest['prompt_hashes'].items():
        assert file_hash(RUN/'prompts'/f'{stage}.txt')==expected_hash
    for task in manifest['tasks']:
        assert file_hash(task['input_ref'])==task['input_sha256']
        original=read_json(task['input_ref'])
        norm,_=normalize(original)
        assert norm==read_json(task['normalized_ref'])
        assert file_hash(task['normalized_ref'])==task['normalized_sha256']
        name=task['task_id'].replace(':','__')
        planpath=RUN/'extract'/name/'output.json'
        plan=read_json(planpath) if planpath.exists() else None
        generated=RUN/'generate'/name/'output.json'
        for stage in STAGES:
            folder=RUN/stage/name
            if not (folder/'response.json').exists():
                continue
            req=read_json(folder/'request.json')
            assert req['messages'][0]['content']==(RUN/'prompts'/f'{stage}.txt').read_text()
            payload=json.loads(req['messages'][1]['content'])
            if stage=='extract': expected=norm
            elif stage=='generate': expected=generator_input(norm,plan)
            else:
                expected=dict(norm,obligation_plan={k:plan[k] for k in ('obligations','ambiguities')},
                    cases=plan['cases'],candidate_rubric=read_json(generated))
            assert payload==expected
            if (folder/'output.json').exists():
                assert parse_response(read_json(folder/'response.json'))==read_json(folder/'output.json')


def main():
    summarize()
    m=read_json(RUN/'manifest.json')
    verify_artifacts(m)
    c=read_json(RUN/'cost_report.json')
    b=read_json(BASE/'cost_report.json')
    n=read_json(ROOT/'runs/rubric_qwen_v10_10_20261024/cost_report.json')
    assert file_hash(BASE/'manifest.json')==m['baseline_manifest_sha256']
    assert file_hash(BASE/'cost_report.json')==m['baseline_cost_report_sha256']
    review=read_json(RUN/'source_review.json')
    byid={r['task_id']:r for r in review['tasks']}
    assert len(byid)==len(review['tasks'])==10
    assert set(byid)=={t['task_id'] for t in m['tasks']}
    for r in c['tasks']:
        checked=byid[r['task_id']]
        p=RUN/'generate'/r['task_id'].replace(':','__')/'output.json'
        if p.exists():
            assert checked['rubric_sha256']==file_hash(p)
            task=next(t for t in m['tasks'] if t['task_id']==r['task_id'])
            try:
                r['baseline_contract_errors']=validate_contract(read_json(p),read_json(task['input_ref']))
            except (TypeError,KeyError,AttributeError):
                r['baseline_contract_errors']=['malformed_rubric']
        else:
            assert checked['status']=='invalid_generation'
            r['baseline_contract_errors']=['not_generated']
        r['source_review_status']=checked['status']
        r['accepted']=not r['rubric_errors'] and checked['status']=='draft_review_pass_with_notes'
    c['summary']['source_review_pass']=sum(r['source_review_status']=='draft_review_pass_with_notes' for r in c['tasks'])
    c['summary']['baseline_contract_pass']=sum(not r['baseline_contract_errors'] for r in c['tasks'])
    c['summary']['source_and_contract_pass']=sum(r['accepted'] for r in c['tasks'])
    rates=m['price_reference']
    def cost(usage):
        return (usage['prompt_tokens']*rates['input_per_million']+usage['completion_tokens']*rates['output_per_million'])/1e6
    c['estimated_cny_at_baseline_rates']=cost(c['totals'])
    c['max_actual_input_tokens']=max(r['prompt_tokens'] for r in c['requests'])
    c['max_conservative_request_reservation']=max(r['reservation_upper'] for r in c['requests'])
    c['summary']['independent_review_complete']=True
    c['source_review_sha256']=file_hash(RUN/'source_review.json')
    c['raw_outputs_and_stage_inputs_verified']=True
    write_json(RUN/'cost_report.json',c)
    write_json(RUN/'status.json',dict(state='completed_with_findings',full_run=False,summary=c['summary']))
    bs=b.get('summary',{})
    bt=b.get('initial',b.get('totals'))
    if bt is None:
        raise ValueError('baseline_usage_missing')
    ns=n['summary']; nt=n['initial']; s=c['summary']
    planpass=sum(not read_json(RUN/'extract'/t['task_id'].replace(':','__')/'validation.json')['errors']
                 for t in m['tasks'] if (RUN/'extract'/t['task_id'].replace(':','__')/'validation.json').exists())
    lines=['# v11：规范化输入、义务清单与语义案例检查','',
        f"相同10题试跑：{s['generated']}/10生成完整可解析rubric，{s['baseline_contract_pass']}/10通过原有原文引用及严格五字段契约，加入义务ID映射检查后为{s['strict_contract_pass']}/10；独立来源审查暂可保留{s['source_review_pass']}/10，格式与语义同时通过{s['source_and_contract_pass']}/10。",'',
        f"义务及案例结构检查通过{planpass}/10；模型语义检查建议保留{s['model_semantic_candidates']}/10。模型建议与独立复核分开计算，不能直接称为业务准确率。",'',
        '## 实现与比较范围','',
        '五字段交付不变。接口示例与规范性属性分离，重建原结构检查没有丢失属性；引用目录回溯原始连续文本。每题先提取带原文的义务及假设案例，再只用义务生成rubric，最后用独立请求推演案例。案例未提供给生成步骤；历史审查参考、旧rubric和逐题反馈未进入任何模型请求。', '',
        '沿用上一轮Qwen3.5-Flash思考配置：temperature=0，thinking_budget=16384，每步回答上限8000，最多两请求并发。最多30请求、操作方60万token保护上限；单次输入估算加思考、回答和余量小于128000。结构无效的计划停止后续步骤；不修复、不自动重试、不全量运行。', '',
        '这是同一已知开发集上的三项联合改动，增加了模型调用次数；不是单因素消融。案例与预期由同一模型提取，检查请求也使用同一模型并可见预期标签，存在相关错误与确认偏差。独立来源复核用于发现这些问题；未执行真实环境、未测量运行准确率。','',
        '## 与旧版对照','',
        '| 指标（分母均为10题） | v10非思考 | v10思考 | v11流程 |',
        '|---|---:|---:|---:|',
        f"| 完整可解析JSON | {ns['complete_json']} | {bs['complete_json']} | {s['generated']} |",
        f"| 原有严格五字段/引用契约通过 | {ns['strict_contract_pass']} | {bs['strict_contract_pass']} | {s['baseline_contract_pass']} |",
        f"| 再加义务ID映射检查 | 未设此项 | 未设此项 | {s['strict_contract_pass']} |",
        f"| 独立静态语义暂可保留 | {ns['semantic_review_pass']} | {bs['semantic_review_pass']} | {s['source_review_pass']} |",
        f"| 原文格式与语义同时通过 | {ns['accepted']} | {bs['accepted']} | {s['source_and_contract_pass']} |",
        f"| 模型调用 | 10 | 10 | {c['physical_requests']} |",
        f"| 总tokens | {nt['total_tokens']:,} | {bt['total_tokens']:,} | {c['totals']['total_tokens']:,} |",
        f"| 同旧版单价估算费用 | ¥{cost(nt):.6f} | ¥{cost(bt):.6f} | ¥{cost(c['totals']):.6f} |",'',
        'v11严格契约包含新增的义务ID覆盖映射检查；旧版没有此项。停止于计划阶段的题记为未生成，不记为已生成rubric的语义失败，但在10题交付率中保留。','',
        '## 分阶段用量','',
        '| 阶段 | 输入 | 输出（含思考） | 思考子项 | 总计 | 同旧价估算¥ |',
        '|---|---:|---:|---:|---:|---:|']
    for stage in ('extract','generate','review'):
        u=c['stages'][stage]
        lines.append(f"| {stage} | {u['prompt_tokens']:,} | {u['completion_tokens']:,} | {u['reasoning_tokens']:,} | {u['total_tokens']:,} | {cost(u):.6f} |")
    lines += ['',
        f"费用仅为按旧轮已记录的输入¥{rates['input_per_million']}/百万、输出¥{rates['output_per_million']}/百万作可比估算，不是账单。输出已包含思考，不能重复加算；不含Codex开发/复核用量。首次生成与检查成本均已计入，本轮没有收费修复。",'',
        f"本轮未知预留{c['unknown_reserved']:,} tokens；共享账本已确认{c['cumulative_confirmed']:,}，历史及其他未知预留{c['cumulative_unknown_reserved']:,}。未使用的预算不是追加调用授权。",'',
        f"单次最大实际输入{c['max_actual_input_tokens']:,} tokens；输入字节保守估算加思考、回答和余量的最大值{c['max_conservative_request_reservation']:,}，均低于128000。",'',
        '## 逐题结果与独立来源复核','',
        '| 题目 | 五字段/引用/义务映射 | 模型语义建议 | 独立来源审查 |',
        '|---|---|---|---|']
    for r in c['tasks']:
        lines.append(f"| {r['task_id']} | {'通过' if not r['rubric_errors'] else '未通过'} | {'暂可保留' if r['semantic_candidate'] else '未通过或未完成'} | {r['source_review_status']} |")
    for r in c['tasks']:
        v=byid[r['task_id']]
        lines += ['', '### '+r['task_id'],'',v['notes'],'',
                  '自动结构检查：'+('通过' if not r['rubric_errors'] else '；'.join(r['rubric_errors'])),'']
        for f in v.get('findings',[]):
            lines += ['- '+f['item_id']+'：'+f['issue'], '  假设反例（独立纸面复核，非真实执行）：'+f['counterexample']]
    if review.get('conclusion'):
        lines += ['', '## 判断与下一步','',review['conclusion']]
    if (RUN/'checker_review.json').exists():
        checker=read_json(RUN/'checker_review.json')
        lines += ['', '## 语义检查器本身的复核','',
            f"共推演{checker['case_count']}个假设案例，原始结果中{checker['raw_label_agreement_not_accuracy']}条与预期标签一致。这只是模型输出调试计数，案例本身、预期标签及推演均可能出错，不是准确率。两个检查输出还将counterexample键误写为带全角冒号的键，结构门禁拒绝。",'']
        for item in checker['tasks']:
            lines.append('- '+item['task_id']+'：'+item['notes'])
    lines += ['', '## 复核与产物','',
        '新增5项工作流测试与已有4项相关测试通过，覆盖原文转义、默认值保留、示例分离、案例不泄漏给生成步骤、伪造引用、反例缺失、漏检/重复案例、错误预期、无效案例与歧义不能记通过。测试验证程序行为，不证明自然语言标准正确。','',
        '首次网络派发曾被自动审批拒绝；补充公开合成数据逐项来源核验及同一已授权服务地址证明后复核通过。拒绝阶段未发出API请求。','',
        f'[完整五字段阅读版]({RUN}/rubrics_zh.md) · [独立来源审查]({RUN}/source_review.json) · [用量明细]({RUN}/cost_report.json) · [实现说明]({ROOT}/docs/rubric_workflow_v11_zh.md)','',
        '离线重建报告：`agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v11.py`。','']
    (RUN/'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps(dict(summary=c['summary'],tokens=c['totals']['total_tokens'],estimated_cny=c['estimated_cny_at_baseline_rates']),ensure_ascii=False))


if __name__=='__main__': main()
