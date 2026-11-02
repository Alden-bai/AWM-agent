"""Offline rendering and shared-budget reconciliation; no API calls."""
import json,sqlite3
from collections import Counter
from pathlib import Path
from rubric_lean import ROOT,BASE,RUN,read_json,write_json,file_hash
def main():
 m=read_json(RUN/'manifest.json');phases={}
 for label,folder in [('initial',RUN),('repair1',RUN/'repair1'),('patch1',RUN/'patch1')]:
  u=Counter();count=0
  for p in folder.glob('*/response.json'):
   r=read_json(p);u.update({k:v for k,v in r['usage'].items() if isinstance(v,int)});count+=1
  phases[label]={'requests':count,'usage':dict(u)}
 with sqlite3.connect(BASE/'budget.sqlite') as db:
  db.row_factory=sqlite3.Row;ledger=[dict(r) for r in db.execute('select * from requests')]
 assert not any(x['status']=='in_flight' for x in ledger)
 known=sum(json.loads(x['usage'])['total_tokens'] for x in ledger if x['usage'])
 reserved=sum(x['reserved'] for x in ledger if not x['usage']);total=known+reserved
 spent=sum(x['usage']['total_tokens'] for x in phases.values())
 assert spent==sum(json.loads(x['usage'])['total_tokens'] for x in ledger if x['request_key'].startswith('lean_') and x['usage'])
 assert total<=800000
 cost=dict(phases=phases,experiment_tokens=spent,experiment_requests=sum(x['requests'] for x in phases.values()),all_history_known_tokens=known,unknown_reserved_tokens=reserved,cumulative_accounted_tokens=total,cap=800000,remaining=800000-total,price_verified=False,currency_cost=None,codex_review_tokens_included=False)
 write_json(RUN/'cost_report.json',cost)
 reviews=read_json(RUN/'source_review_final.json');byid={x['task_id']:x for x in reviews['tasks']}
 lines=['# 低成本试验：10题完整五字段rubric','','模型输出经本地展开的草案，含已知问题；未替换v7。没有运行任务或实际检查器。','']
 def cell(x):return str(x).replace('|','\\|').replace('\n','<br>')
 for t in m['tasks']:
  key=t['task_id'];d=RUN/'final'/key.replace(':','__');x=read_json(d/'rubric.json');source=read_json(Path(t['original_ref']))
  rv=byid[key];origin=read_json(d/'origin.json')
  lines+=['## '+key,'',source['task'],'','静态审查：'+rv['status']+'。'+rv.get('finding',''),'']
  if 'patch_rejected' in origin:lines+=['补丁被拒绝，保留上一轮有效结构：'+origin['reason'],'']
  lines+=['| 编号 | 评价标准 | 原文或规则依据 | 必要/偏好 | 需要的证据 | 判定方式 |','|---|---|---|---|---|---|']
  for i in x['rubric']:
   refs='<br>'.join(a['source_id']+'：'+a['quote'] for a in i['sources'])
   ev='<br>'.join(a['description'] for a in i['evidence']);dec=i['decision']
   decision='<br>'.join(a+'：'+dec[b] for a,b in [('方法','method'),('满足','satisfied_when'),('违反','violated_when'),('未知','unknown_when')])
   lines.append('| '+' | '.join(cell(v) for v in [i['id'],i['criterion'],refs,i['requirement_level'],ev,decision])+' |')
  lines+=['','未决问题：'+json.dumps(x['unresolved'],ensure_ascii=False),'']
 (RUN/'rubrics_zh.md').write_text('\n'.join(lines))
 baseline=read_json(BASE/'cost_report.json')['by_version'];old1=baseline['v1']['usage']['total_tokens'];old7=baseline['v7']['usage']['total_tokens']
 initial=phases['initial']['usage']['total_tokens'];accepted=sum(x['status']=='draft_review_pass_with_notes' for x in reviews['tasks'])
 status=dict(status='bounded_cost_experiment_complete_quality_regressed',cost=cost,structure_valid_final=10,rejected_patches=3,accepted_source_drafts=accepted,total_tasks=10,runtime_cases_executed=0,full_run=False,promote_to_default=False,previous_v7_preserved=True)
 write_json(RUN/'status.json',status)
 text='# 同10题低成本试验\n\n成本下降，但质量回退；不替换v7，不做全量。\n\n之前754,117 tokens是七轮试验加未知用量预留，不是一次生成10题。之前70次响应确认680,981 tokens，另有73,136预留。\n\n| 本次阶段 | 请求数 | 输入tokens | 输出tokens | 合计 |\n|---|---:|---:|---:|---:|\n'
 for label,p in phases.items():
  u=p['usage'];text+=f"| {label} | {p['requests']} | {u['prompt_tokens']:,} | {u['completion_tokens']:,} | {u['total_tokens']:,} |\n"
 text+=f"""
本次共 **{spent:,} tokens**，相对旧v7单轮{old7:,}减少 **{1-spent/old7:.1%}**；相对最初v1独立生成{old1:,}减少 **{1-spent/old1:.1%}**。仅本次首轮{initial:,}相对最初v1减少{1-initial/old1:.1%}，但首轮语义审查0/10，不能只报便宜数字。

累计已确认{known:,}，未知预留{reserved:,}，预算占用 **{total:,}/800,000**，剩余{800000-total:,}。共享原SQLite账本，逐请求保守预留，未重置预算、无自动重试。保持deepseek-flash、temperature=0。只计DeepSeek请求，不含本对话Codex审查成本；未核验货币价格。

按本样本线性外推一万题：首轮约{initial*1000:,} tokens；若需要同样比例的修复约{spent*1000:,} tokens。这不是全量报价，不含自动审查或运行检查器；样本非随机、不同题上下文长度会变化。

## 节省来源

1. 本地提取参数含义、关键返回语义和完整DDL约束，去掉示例、重复元数据、工具清单。数据包约4–8KB，没有LLM压缩费用。
2. 模型输出数字source IDs，本地回填冻结原文，保留provenance。
3. 模型只写一次谓词P和语义前提G，本地展开G且P通过、G且非P失败、非G未知。这是文字结构约束，不是可执行验证。
4. 首轮不带旧稿/逐题反馈；修复只带原题、少量相关事实、核实的错误。最后只补丁6题的指定条目，另外4题不重跑。
5. 缓存已有完整响应，未知用量请求不重试；非法补丁原子拒绝，保留前一有效结构。

## 质量与限制

首轮10/10结构合法但0/10通过来源语义审查。短修复后对6题发补丁，其中3份将criterion改成空字符串，被拒绝。最终10份结构完整草案中3份回退到补丁前结果，不把回退算成补丁通过。

最终静态审查暂可保留 **{accepted}/10**，之前v7为8/10。这是Codex源文对照和纸面反例审查，不是执行准确率。后续修复使用逐题反馈，不能宣称短prompt单独达到了该质量。

| 题目 | 静态审查 | 问题或保留意见 |
|---|---|---|
"""
 for row in reviews['tasks']:text+=f"| {row['task_id']} | {row['status']} | {cell(row.get('finding',''))} |\n"
 text+=f"""
## 产物与验证

- [项目skill]({ROOT/'skills/awm-rubric-efficient/SKILL.md'})，通过项目.agents/skills下同名链接发现。
- [首轮prompt]({ROOT/'configs/rubric/generate_lean_v1.txt'})、[修复prompt]({ROOT/'configs/rubric/repair_lean_v1.txt'})、[局部补丁prompt]({ROOT/'configs/rubric/patch_lean_v1.txt'})。
- [输入压缩与五字段展开]({ROOT/'scripts/rubric_lean.py'})、[短修复]({ROOT/'scripts/rubric_lean_repair.py'})、[补丁应用]({ROOT/'scripts/rubric_lean_patch.py'})。
- [完整10题]({RUN/'rubrics_zh.md'})、[来源审查]({RUN/'source_review_final.json'})、[账目]({RUN/'cost_report.json'})。

3项本地单元检查通过：参数规则/DDL约束/原文溯源保留、预算与输入隔离、判定展开及非法引用拒绝。Skill格式校验通过。未运行正负执行轨迹。

保留压缩、引用回填、缓存和局部补丁工程能力，但当前短prompt不设为全量默认。下一步应固定新题，比较“仅压缩输入且保留强约束”与“激进精简输出”的质量/成本。本轮没有启动该额外试验。旧v7成果保留。
"""
 (RUN/'report_zh.md').write_text(text)
 paths=[ROOT/'scripts/rubric_lean.py',ROOT/'scripts/rubric_lean_repair.py',ROOT/'scripts/rubric_lean_patch.py',ROOT/'scripts/summarize_rubric_lean.py',ROOT/'skills/awm-rubric-efficient/SKILL.md']
 paths += [p for p in RUN.rglob('*') if p.is_file() and p.name in {'response.json','input.json','provenance.json','prompt.txt','manifest.json','source_review_final.json','cost_report.json','report_zh.md'}]
 write_json(RUN/'checksums.json',{str(p.relative_to(ROOT)):file_hash(p) for p in sorted(paths)})
 print(json.dumps(status,ensure_ascii=False))
if __name__=='__main__':main()
