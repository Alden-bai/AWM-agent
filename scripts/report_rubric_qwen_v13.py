"""Offline rendering of v13 acceptance, revisions, and all-attempt costs."""
import json
from rubric_qwen_v13 import RUN, DIMENSIONS, read_json, write_json, file_hash


def main():
    report=read_json(RUN/'cost_report.json');m=report['metrics'];manifest=read_json(RUN/'manifest.json')
    per_pass='无定义（没有通过版）' if m['cost_per_passed_rubric_cny'] is None else f"¥{m['cost_per_passed_rubric_cny']:.6f}"
    initial=sum(s['known_estimated_cny'] for k,s in report['stages'].items() if k.startswith('initial_'))
    revision=sum(s['known_estimated_cny'] for k,s in report['stages'].items() if k.startswith('revision_'))
    repair_kinds={}
    terminal_counts={}
    for state in report['tasks']:terminal_counts[state['status']]=terminal_counts.get(state['status'],0)+1
    for path in (RUN/'tasks').glob('*/round_*/generate/request.json'):
        payload=json.loads(read_json(path)['messages'][1]['content'])
        kind=payload.get('revision_feedback',{}).get('kind')
        if kind:repair_kinds[kind]=repair_kinds.get(kind,0)+1
    text=['# v13：全Qwen生成、全新上下文审查与两轮以内修订','',
        f"首次通过{m['first_pass']}/10（{m['first_pass_rate']:.0%}），最终通过{m['final_pass']}/10（{m['final_pass_rate']:.0%}）。"
        f"本轮已确认用量估算¥{m['known_estimated_cny']:.6f}，每份通过rubric费用{per_pass}。",'',
        '上述通过数仅按程序格式/原文检查及Qwen五维有效评分均非0判定；没有额外静态语义复核门槛。'
        '1分允许通过，不要求全2分。通过不等同于运行正确率或人工评估的质量准确率。','',
        '## 主指标','',
        '| 指标 | 本轮 |','|---|---:|',
        f"| 首次通过率 | {m['first_pass']}/10 = {m['first_pass_rate']:.0%} |",
        f"| 最终通过率 | {m['final_pass']}/10 = {m['final_pass_rate']:.0%} |",
        f"| 修订新增通过题数 | {m['final_pass']-m['first_pass']} |",
        f"| 每份通过rubric费用（全部已确认费用÷最终通过数） | {per_pass} |",
        f"| 初稿生成与首次审查费用 | ¥{initial:.6f} |",
        f"| 修订生成与重审费用 | ¥{revision:.6f} |",
        f"| 实际请求 | {m['physical_requests']} |",
        f"| 已确认tokens | {m['confirmed_tokens']:,} |",
        f"| 用量未知预留tokens | {m['unknown_reserved_tokens']:,} |",'',
        '分母固定为原始10题，结构失败、未完成审查、两轮后未通过的题仍计入。费用分子包括所有失败尝试与修订，'
        '不只统计成功题，也不把思考token重复计入输出。所有原题保留。','',
        f"修订调用按触发原因：结构/映射反馈{repair_kinds.get('structure_errors',0)}次，"
        f"五维审查反馈{repair_kinds.get('judge_feedback',0)}次。两类均消耗同一两轮额度。",'',
        ('本批次没有有效含0分审查进入语义修订：出现0分的审查同时带有格式错误，按冻结规则停止。'
         '所以这批端到端结果验证了结构修订和无0通过门槛；语义反馈修订分支已通过单元测试，'
         '但尚未由本批实际API结果验证其效果。' if repair_kinds.get('judge_feedback',0)==0 else
         '有效含0评分已进入语义反馈修订；各轮评分和修订调用均单独保留。'),'',
        '最终状态计数：'+json.dumps(terminal_counts,ensure_ascii=False)+'。审查无效的题保留原始分数和校验错误，'
        '不能将不完整或错误引用的审查作为合法通过结果。','',
        f"用量未知请求的保守费用预留为¥{m['unknown_reserved_cny']:.6f}，与已确认估算分开；"
        '若该值非0，每份通过费用只代表已确认部分。价格估算不是实付账单，不包含Codex开发费用。','',
        '## 逐题轮次与五维评分','',
        '维度顺序：来源支持 / 题意覆盖 / 硬软分类 / 证据设计 / 判定逻辑。轮次0是初稿，1和2是修订。','',
        '| 题目 | 初稿 | 修订1 | 修订2 | 最终状态 | 该题已确认费用 |',
        '|---|---|---|---|---|---:|']
    artifacts=['# v13最终通过的五字段rubric','',
        '通过依据：原文/结构检查通过，且本版全新上下文的Qwen五维有效评分均不为0。','']
    detail=[]
    for state,t in zip(report['tasks'],manifest['tasks']):
        task_id=t['task_id'];name=task_id.replace(':','__');folder=RUN/'tasks'/name
        cells=[]
        for n in range(3):
            d=folder/f'round_{n}'
            if (d/'evaluation.json').exists():
                e=read_json(d/'evaluation.json')
                cells.append('/'.join(str(e['scores'].get(dim,'?')) for dim in DIMENSIONS)+' '+e['status'])
                if e['errors']:
                    detail+=['### '+task_id+f' / 轮次{n} 审查格式失败','',
                        '错误：'+json.dumps(e['errors'],ensure_ascii=False),'']
            elif (d/'judge'/'request_identity.json').exists():cells.append('审查未完成/无效')
            elif (d/'validation.json').exists():cells.append('结构未通过')
            elif (d/'generate'/'request_identity.json').exists():cells.append('生成未完成')
            else:cells.append('—')
        cost=sum(r.get('estimated_cny',0) for r in report['requests'] if r['task_id']==task_id)
        text.append('| '+task_id+' | '+' | '.join(cells)+f" | {state['status']} | ¥{cost:.6f} |")
        if state['status']=='passed':
            r=state['accepted_revision'];accepted=RUN/'accepted'/f'{name}.json'
            rubric=read_json(accepted); scores=read_json(folder/f'round_{r}'/'evaluation.json')['scores']
            artifacts+=['## '+task_id,'',read_json(t['input_ref'])['task'],'',
                f"通过轮次：{r}；五维评分："+json.dumps(scores,ensure_ascii=False),'',
                '```json',json.dumps(rubric,ensure_ascii=False,indent=2),'```','']
        for n in range(3):
            d=folder/f'round_{n}'
            if (d/'judge'/'output.json').exists():
                j=read_json(d/'judge'/'output.json')
                detail+=['### '+task_id+f' / 轮次{n}','']
                for dimension in j.get('dimensions',[]):
                    detail.append(f"- {dimension['name']}={dimension['score']}：{dimension['reason']}")
                detail+=['']
    text+=['','## 分阶段成本','',
        '| 阶段 | 调用数 | 已确认估算费 |','|---|---:|---:|']
    for k,s in report['stages'].items():text.append(f"| {k} | {s['calls']} | ¥{s['known_estimated_cny']:.6f} |")
    text+=['',
        'Qwen北京128k内普通输入按¥0.2/百万、输出按¥2/百万估算，不假定缓存折扣。'
        '[官方价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)。','',
        '上一轮v12使用Qwen生成和DeepSeek审查：18次请求，已确认估算¥0.506137，另有约¥0.090026未知预留；'
        '未产出通过版。本轮换了审查模型、接受含1分的评分，并新增最多两轮修订，不能把通过率差异归因于单一因素。','',
        '## 上下文隔离与128k核验','',
        '每个生成/审查调用都只有system与user两条消息。每次审查只含原题、候选、公开来源、证据说明及当前rubric；'
        '没有上一轮分数、审查意见、旧版rubric或生成思考。修订生成器会收到上一版输出和反馈，重审不会收到。'
        '汇总脚本逐请求重建payload，并与实际发送记录核对，不仅检查提示词声明。','',
        f"最大单次保守输入+输出/思考预留为{m['max_request_guard']:,} tokens；"
        f"最大已知实际输入{m['max_actual_input']:,} tokens，均小于128000。"
        '网络错误没有自动重发；所有已通过题均停止后续轮次。','',
        '8项新增测试通过，覆盖无0即通过、非法评分拒绝、修订上限、通过后停止、结构错误占用轮次及重审上下文隔离。'
        '已核对源文件哈希、实际请求身份、模型响应、原文装配、共享账本usage和接受版与对应轮次一致。','',
        '## 各轮审查理由摘要','']+detail
    (RUN/'report_zh.md').write_text('\n'.join(text))
    (RUN/'accepted_rubrics_zh.md').write_text('\n'.join(artifacts))
    write_json(RUN/'report_provenance.json',dict(cost_report_sha256=file_hash(RUN/'cost_report.json'),
        manifest_sha256=file_hash(RUN/'manifest.json'), report_sha256=file_hash(RUN/'report_zh.md')))
    print(json.dumps(m,ensure_ascii=False))


if __name__=='__main__':main()
