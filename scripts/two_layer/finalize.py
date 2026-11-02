"""Recompute the original-100 comparison and audit every retained task from raw files."""
import collections, json, sqlite3, sys
from pathlib import Path
from run import *


def finalize(run):
 run=Path(run);m=read_json(run/'manifest.json')
 # Confirmed local environment defects override only their directly dependent tasks.
 if (run/'confirmed_defects.jsonl').exists():
  for finding in clean_data.rows(run/'confirmed_defects.jsonl'):
   task=next(t for t in m['tasks'] if t['ledger_id']==finding['ledger_id']);root=run/'tasks'/finding['ledger_id'];root.mkdir(exist_ok=True)
   old=read_json(root/'result.json') if (root/'result.json').exists() else {}
   if old.get('state')!='reject' and old:save_json(root/'before_confirmed_defect.json',old)
   save_json(root/'result.json',{**old,**task,**finding,'finished_at':now()})
 report(run)
 records=clean_data.rows(run/'tasks.ledger.jsonl');summary=read_json(run/'summary.json');cost=read_json(run/'cost_report.json')
 with sqlite3.connect(run/'budget.sqlite') as db:
  request_records={key:{'stage':stage,'status':status} for key,stage,status in db.execute('SELECT key,stage,status FROM requests')}
 referenced_request_keys=set()
 audits=[];case_rows=[];slot_rows=[];requirements=[];deps=[];tool_checks=[];coverage_rows=[]
 attempted_tasks=set();valid_positive_tasks=set();active_case_count=0
 for r in records:
  base=Path(r.get('evidence_dir',run/'tasks'/r['ledger_id']))
  if (base/'config.json').exists():specfile=base/'config.json';execution=base/'execution'
  else:execution=run/'tasks'/r['ledger_id']/f"round_{r.get('last_round',0)}";specfile=execution/'requirements.json'
  spec=read_json(specfile) if specfile.exists() else {}
  for req in spec.get('requirements',[]):requirements.append({'ledger_id':r['ledger_id'],**req,'source':str(specfile),'spec_sha256':digest(spec)})
  deps.append({'ledger_id':r['ledger_id'],'scenario':r['scenario'],'status':r.get('layer1'),'tools':sorted({c['tool'] for c in spec.get('positive',{}).get('calls',[])}),'source':str(specfile)})
  for cfile in sorted((execution/'cases').glob('*/evaluated.json')):
   c=read_json(cfile);active_case_count+=1;attempted_tasks.add(r['ledger_id'])
   if c.get('expected') and c.get('label_valid'):valid_positive_tasks.add(r['ledger_id'])
   case_rows.append({'ledger_id':r['ledger_id'],'case_id':c['name'],'expected':c['expected'],'label_valid':c['label_valid'],'violated_requirements':c.get('violates',[]),'source':str(cfile),'source_sha256':digest(cfile),'artifact_status':'active_configuration_attempt'})
   for tc in c.get('tool_checks',[]):tool_checks.append({'ledger_id':r['ledger_id'],'case_id':c['name'],**tc,'source':str(cfile)})
  for cov in r.get('coverage',[]):coverage_rows.append({'ledger_id':r['ledger_id'],**cov,'source':str(execution/'coverage.json')})
  for labelpath in base.glob('*/label.json'):
   c=read_json(labelpath)
   case_rows.append({'ledger_id':r['ledger_id'],'case_id':c['name'],'expected':c['expected'],'label_valid':c['label_valid'],'violated_requirements':c.get('violated',[]),'source':str(labelpath),'source_sha256':digest(labelpath),'artifact_status':'targeted_output_recheck'})
  errors=[];actuals=[]
  for c in r.get('judged_cases',[]):
   for slot in c['slots']:
    path=Path(c['path'])/'judge'/str(slot['slot']);requestpath=path/'official_request.json'
    if requestpath.exists():
     req=read_json(requestpath);content=req['messages'][1]['content'];taskpart,verificationpart=content.split('\n\nverification_json:\n',1);payload=json.loads(taskpart.removeprefix('task_json:\n'));sql=json.loads(verificationpart)
     trace=read_json(Path(c['path'])/'trajectory.json')
     input_ok=payload['user_task']==r['task'] and payload['trajectory']==trace['trajectory'] and any(row.get('is_final') and row.get('content')==trace['final_answer'] for row in payload['trajectory'])
     if not input_ok:errors.append('input_mismatch:'+c['name']+':'+str(slot['slot']))
     keys=[key for key in (str(path.resolve()),str((path/'transport_resume_1').resolve())) if key in request_records]
     referenced_request_keys.update(keys)
     rec={'ledger_id':r['ledger_id'],'case_id':c['name'],'expected':c['expected'],'slot':slot['slot'],'dispatched':bool(keys),'attempts':[{'key':key,**request_records[key]} for key in keys],'valid':slot['valid'],'accepted':slot['accepted'],'classification':slot['classification'],'finish_reason':slot.get('finish_reason'),'input_exactly_matches_real_trace':input_ok,'official_request_sha256':digest(requestpath),'response_sha256':digest(path/'response.json') if (path/'response.json').exists() else None,'source':str(path)}
     slot_rows.append(rec);actuals.append(rec)
   if r['state']=='keep' and (len(c['slots'])!=5 or {s['slot'] for s in c['slots']}!=set(range(5)) or not all(s['valid'] and s['accepted']==c['expected'] for s in c['slots'])):errors.append('five_slots_failed:'+c['name'])
  if r['state']=='keep':
   freeze=read_json(base/'frozen_formal_cases.json')
   if len(freeze['cases'])!=len(r.get('judged_cases',[])):errors.append('formal_case_count_mismatch')
   if not r.get('judged_cases'):errors.append('no_judge_cases')
   if any(c.get('status')!='covered' for c in r.get('coverage',[])):errors.append('coverage_missing')
   if not read_json(execution/'positive_replay/evaluated.json')['passed']:errors.append('replay_failed')
   audits.append({'ledger_id':r['ledger_id'],'passed':not errors,'errors':errors,'formal_cases':len(freeze['cases']),'slots':len(actuals),'boundary_review':str(run/'tasks'/r['ledger_id']/'root_boundary_review.json') if (run/'tasks'/r['ledger_id']/'root_boundary_review.json').exists() else None,'tested_case_paths':[c['path'] for c in freeze['cases']]})
   assert not errors,(r['ledger_id'],errors)
 write_rows(run/'keep_audit.jsonl',audits);write_rows(run/'cases.jsonl',case_rows);write_rows(run/'judge_results.jsonl',slot_rows);write_rows(run/'requirements.jsonl',requirements);write_rows(run/'task_dependencies.jsonl',deps);write_rows(run/'tool_checks.jsonl',tool_checks);write_rows(run/'coverage.jsonl',coverage_rows)
 # Deduplicate semantic recipes for reporting. Repeated slots/cases never become independent tasks.
 input_groups=collections.defaultdict(list)
 for r in records:
  for c in r.get('judged_cases',[]):
   if not any(str((Path(c['path'])/'judge'/str(s['slot'])).resolve()) in request_records for s in c['slots']):continue
   trace=read_json(Path(c['path'])/'trajectory.json')
   signature={'task':r['task'],'answer':trace['final_answer'],'calls':[tc['arguments'] for row in trace['trajectory'] for tc in row.get('tool_calls',[]) if tc['name']=='call_tool']}
   input_groups[digest(signature)].append((r['ledger_id'],c['name']))
 old=read_json(ROOT/'evidence/legacy/cleaning/summary.json')
 all_execution_ids=set()
 for parent in ('tasks','confirmed_environment_defects'):
  for tracepath in (run/parent).glob('*/**/trajectory.json'):
   tr=read_json(tracepath)
   if tr.get('ledger_id') and any(tc.get('name')=='call_tool' for row in tr['trajectory'] for tc in row.get('tool_calls',[])):all_execution_ids.add(tr['ledger_id'])
 certs=clean_data.rows(ROOT/'evidence/legacy/cleaning/task_certificates.jsonl')
 oldstates=collections.Counter(c['decision'] for c in certs if c['ledger_id'] in m['pilot100'])
 transitions=collections.Counter((next(c['decision'] for c in certs if c['ledger_id']==r['ledger_id']),r['state']) for r in records)
 # Current and superseded judge attempts are shown separately, while all costs remain cumulative.
 current_judge_slots=sum(s['dispatched'] for s in slot_rows)
 current_physical_judge_requests=len(referenced_request_keys)
 with sqlite3.connect(run/'budget.sqlite') as db:
  total_judge_requests=db.execute("SELECT count(*) FROM requests WHERE stage='judge'").fetchone()[0]
  physical=db.execute('SELECT count(*) FROM requests').fetchone()[0]
  running=db.execute("SELECT count(*) FROM requests WHERE status='started'").fetchone()[0]
  unknown_rows=db.execute("SELECT count(*),coalesce(sum(reserved),0) FROM requests WHERE status IN ('interrupted_unknown','started')").fetchone()
  rejected_requests=db.execute("SELECT count(*) FROM requests WHERE status='rejected_authorization'").fetchone()[0]
  reserved_total=db.execute('SELECT coalesce(sum(reserved),0) FROM requests').fetchone()[0]
 summary.update(old_counts=dict(oldstates),status_transitions=[{'old':o,'new':n,'tasks':v} for (o,n),v in sorted(transitions.items())],actual_case_execution_tasks=len(attempted_tasks),tasks_with_mechanically_valid_positive=len(valid_positive_tasks),active_candidate_cases=active_case_count,unique_current_judged_inputs=len(input_groups),repeated_identical_case_inputs=sum(len(v)-1 for v in input_groups.values()),verified_keep_tasks=len(audits),verified_keep_cases=sum(x['formal_cases'] for x in audits),verified_keep_slots=sum(x['slots'] for x in audits),current_judge_slots=current_judge_slots,current_physical_judge_requests=current_physical_judge_requests,total_physical_judge_requests=total_judge_requests,superseded_or_unreferenced_judge_requests=total_judge_requests-current_physical_judge_requests,running_requests=running,all_original_tasks_have_result=all((run/'tasks'/t['ledger_id']/'result.json').exists() for t in m['tasks']))
 save_json(run/'summary.json',summary)
 summary['actual_case_execution_tasks_all_attempts']=len(all_execution_ids)
 save_json(run/'summary.json',summary)
 cost['cumulative_tokens_including_successful_preflight']=cost['tokens'].get('total_tokens',0)+70
 cost['preflight_network_attempts']=2;cost['preflight_delivered_responses']=1;cost['preflight_local_network_attempt']='Initial sandbox DNS connection stalled and was terminated with exit 130; no response or reported usage. Separate successful connectivity response used 70 tokens.'
 cost['superseded_judge_requests_still_in_cost']=total_judge_requests-current_physical_judge_requests
 cost['usage_unknown_requests']=unknown_rows[0];cost['usage_unknown_reserved_token_upper_bound']=unknown_rows[1];cost['authorization_rejected_requests']=rejected_requests;cost['conservative_tokens_including_unknown_and_preflight']=reserved_total+70
 cost['authorized_transport_resumption_attempts']=sum(key.endswith('/transport_resume_1') for key in request_records)
 cost['transport_resumption']='One new attempt per interrupted/rejected request explicitly authorized after account top-up; all earlier attempts retained. No valid Judge response resampled.'
 save_json(run/'cost_report.json',cost)
 counts=summary['new_counts'];validpos=summary['judge_metrics'].get('valid_positive',0);validneg=summary['judge_metrics'].get('valid_negative',0)
 lines=['# 原100题两层清洗：实测对比','',f"同一批 **100道题 / 10个环境**，同一数据版本 `{m['revision']}`，清单与原顺序完全一致。",'', '本报告把任务是否通过、案例判分是否正确、工程是否补齐分开统计。`keep` 只代表本次有限测试通过。','', '| 任务级结果（分母均为100题） | 旧严格认证 | 新两层流程 |','|---|---:|---:|',f"| 保留 keep | {oldstates['keep']} | {counts.get('keep',0)} |",f"| 确证缺陷 reject | {oldstates['reject']} | {counts.get('reject',0)} |",f"| 尚未通过 | {oldstates['quarantine']} | {sum(counts.get(x,0) for x in ['review','pending_build','blocked_run'])} |",'',f"尚未通过细分：工程待补 `{counts.get('pending_build',0)}`，需语义/条件复核 `{counts.get('review',0)}`，运行阻断 `{counts.get('blocked_run',0)}`。review包括模型提出但尚未由独立事实消解的解释分歧，不能当成已确认题意错误。",'', '旧结构筛查曾给出90题retain、10题reject，但其semantic_certified_tasks=0；retain仅是候选。上表使用旧严格认证的keep口径，避免把90个候选与4个通过有限认证的任务混为一谈。', '', '## 实际增加了什么', '',f"- 完成逐题两份条件提取：{summary['dual_requirements_extracted']}/100；旧流程只有1题完整插件，其余89题存在逐题证据欠账。",f"- 发生真实工具调用（含历史构造尝试和缺陷探针）：{len(all_execution_ids)}/100题；当前候选配置有执行记录{len(attempted_tasks)}题。有至少一个满足两份可执行条件的正例：{len(valid_positive_tasks)}/100题。这两项都不等同keep。",f"- 最终保留项逐一复核：{len(audits)}题、{summary['verified_keep_cases']}个正式案例、{summary['verified_keep_slots']}个固定Judge slot，全部有效且符合冻结标签。",'- 重新复现原workflow_automation_4初始化错误，原10题继续排除。', '- 新复现education_training_management_1的5道任务：目标学徒552属于用户2，工具固定用户1。题2、3、6写入学徒0的占位记录；题7汇总返回假零值，题9无法正确验证已满足的发证前提。均在两份新数据库复现，只排除直接依赖的这5题。' , '- 新复现application_management_1:9判分缺陷：任务明确要求返回申请人ID和缺失/无效项，最终答案为空对象{}仍被官方Judge接受。已核对原生输入确实含空答案、工具证据和数据库参考正确；按确证缺陷提前停止，没有凑满5次或重采样。', '', '## 保留题目', '', '| 任务 | 有限测试范围 |','|---|---|']
 for r in records:
  if r['state']=='keep':lines.append(f"| {r['scenario']}:{r['task_idx']} | {len(r['judged_cases'])}个案例，每例5次；条件、实际工具/DB、重放与输入均有证据 |")
 lines+=['','## 官方SQL+LLM案例级结果','',f"当前版本实际派发：{len(input_groups)}个不同输入；另有{summary['repeated_identical_case_inputs']}个重复配方，不能当独立样本。",f"- 正例有效slot：{validpos}；已复核误拒绝：{summary['judge_metrics'].get('false_reject',0)}/{validpos}。",f"- 负例有效slot：{validneg}；已复核误接受：{summary['judge_metrics'].get('false_accept',0)}/{validneg}。",f"- 实际收到但输出无效：{summary['judge_metrics'].get('invalid_response',0)}；已发出但未取得有效响应：{summary['judge_metrics'].get('transport_or_usage_unknown',0)}；未派发slot：{summary['judge_metrics'].get('not_dispatched',0)}。至少一次原始分歧涉及{summary['cases_with_disagreement']}个案例、{summary['tasks_with_disagreement']}题。",f"- 全过程实际Judge请求{total_judge_requests}次；其中{total_judge_requests-current_physical_judge_requests}次属于已被新版案例取代或尚未纳入当前结论的尝试，仍全部纳入用量账目；349次中包含13次HTTP 402拒绝，不能视为349次有效判定。",'', '分母只含已核实、实际派发且输出有效的案例判定，不能代表全部100题或全库。5个slot是相关重复调用。旧216/224来自另一套历史构造案例和待复核标签，因此不能与本轮直接相减宣称准确率提升。', '', '## 成本与工程结论','',f"累计正式流程请求{physical}次，已报告token {cost['tokens'].get('total_tokens',0):,}；加成功连通性检查70 tokens，共{cost['cumulative_tokens_including_successful_preflight']:,}。用户授权累计上限12,000,000 tokens、原100题范围；金额没有可靠单价，未估造。",f"另有{unknown_rows[0]}次请求因并发中断没有收到usage，保留{unknown_rows[1]:,} tokens保守预留；连同这些未知用量与70 tokens连通检查，预算占用上界为{reserved_total+70:,}。HTTP 402拒绝共{rejected_requests}次，恢复授权及旧失败记录均保留。",'', '当前剩余预算上界空间27,286 tokens，已不足以按既定预留规则启动下一项补全请求。本轮在断点停止模型派发；100题均有账目，仍有80题未取得保留或缺陷结论。', '', '通用模型构造仍会添加题面未要求的条件、混淆初态和新记录、使用不存在的错对象、遗漏真实返回结构。共享模板修复帮助简单新增/字段修改题形成闭环，但复杂批量、时间范围、输出范围题仍需要补全。不能把本次结果宣传为已完成100题语义认证，也不能把review/pending改名当作清洗提升。', '', '## 逐题对比','', '| 题目 | 旧状态 | 新状态 | 原因/下一步 |','|---|---|---|---|']
 oldmap={c['ledger_id']:c['decision'] for c in certs}
 for r in records:
  reason='; '.join(r.get('reasons',[])).replace('|','/');lines.append(f"| {r['scenario']}:{r['task_idx']} | {oldmap[r['ledger_id']]} | {r['state']} | {reason} |")
 lines+=['','## 复现与证据','', '- manifest.json：原100题顺序、七类资产哈希、官方源码和模型参数。', '- implementation_amendment_*.json、各题worker_source.json：实现修复过程；旧原始结果保留。', '- requirements.jsonl、coverage.jsonl、task_dependencies.jsonl、tool_checks.jsonl：逐题条件/覆盖/依赖证据。', '- cases.jsonl、judge_results.jsonl、keep_audit.jsonl：实际案例、原生Judge输入校验及5-slot验收。', '- tasks.ledger.jsonl及各状态清单、build_queue.jsonl、review_queue.jsonl：完整100题账目与未完成原因。', '- tasks/*、confirmed_environment_defects/*：初末DB、真实工具响应、两轮提取、修补、复核、每次请求响应。', '- cost_report.json、budget.sqlite：全部阶段物理请求、tokens、旧版本成本和预算授权。','', '正式判分只使用原版SQL+LLM。为兼容既有DeepSeek接口，传输层将参数名max_completion_tokens映射为max_tokens，数值保持4096；原始参数和实际上网请求分别保存。模型名沿用deepseek-flash，并不代表供应商底层模型快照不可变。原始数据和官方Judge未修改。']
 (run/'report_zh.md').write_text('\n'.join(lines)+'\n')
 print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':finalize(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'cleaning_runs/two_layer_pilot100_v1')
