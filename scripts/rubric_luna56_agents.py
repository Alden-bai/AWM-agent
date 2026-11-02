"""Offline source binding, score checks and usage reporting for delegated agents.

This script never calls any model. Agent orchestration is explicit in the chat.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from rubric_pilot import ROOT, read_json, write_json, file_hash
from rubric_candidates import prepare_input, validate_selection, compile_rubric
from rubric_qwen_v13 import evaluate

RUN=ROOT/'runs/rubric_luna56_agents_10_20261024'


def compile_round(n):
    m=read_json(RUN/'manifest.json')
    tasks=m['tasks'] if n==0 else read_json(RUN/'revision_plan.json')['tasks']
    rows=[]
    for t in tasks:
        name=t['task_id'].replace(':','__');folder=RUN/f'round_{n}'
        inp=read_json(t['input_ref']);assert file_hash(t['input_ref'])==t['input_sha256']
        view,pack=prepare_input(inp)
        path=folder/'generate'/f'{name}.json'
        try:
            selected=read_json(path); errors=validate_selection(selected,inp,pack)
        except (ValueError,TypeError,KeyError,FileNotFoundError) as exc:
            errors=[type(exc).__name__]
        row=dict(task_id=t['task_id'],errors=errors)
        if not errors:
            rubric=compile_rubric(selected,inp,pack)
            write_json(folder/'compiled'/f'{name}.json',rubric)
            write_json(folder/'judge_input'/f'{name}.json',dict(view,candidate_rubric=rubric))
            row.update(rubric_sha256=file_hash(folder/'compiled'/f'{name}.json'),
                       selection_sha256=file_hash(path))
        write_json(folder/'validation'/f'{name}.json',row);rows.append(row)
    write_json(RUN/f'round_{n}'/'compilation.json',dict(tasks=rows,compiled=sum(not r['errors'] for r in rows)))
    print(json.dumps(dict(round=n,compiled=sum(not r['errors'] for r in rows),tasks=rows),ensure_ascii=False))


def review_round(n):
    folder=RUN/f'round_{n}';rows=[]
    for t in read_json(folder/'compilation.json')['tasks']:
        name=t['task_id'].replace(':','__')
        if t['errors']:
            rows.append(dict(task_id=t['task_id'],status='invalid_generation',errors=t['errors']));continue
        rubric=read_json(folder/'compiled'/f'{name}.json');payload=read_json(folder/'judge_input'/f'{name}.json')
        try:
            judged=read_json(folder/'judge'/f'{name}.json')
            result=evaluate(judged,t['task_id'],{i['id'] for i in rubric['rubric']},{c['candidate_id'] for c in payload['candidates']})
        except (ValueError,TypeError,KeyError,FileNotFoundError) as exc:
            result=dict(status='invalid_judge',errors=[type(exc).__name__],scores={})
        row=dict(task_id=t['task_id'],**result);rows.append(row)
        write_json(folder/'evaluation'/f'{name}.json',row)
    write_json(folder/'results.json',dict(tasks=rows,passed=sum(r['status']=='passed' for r in rows)))
    print(json.dumps(dict(round=n,passed=sum(r['status']=='passed' for r in rows),tasks=rows),ensure_ascii=False))


def revision_plan():
    m=read_json(RUN/'manifest.json');original={t['task_id']:t for t in m['tasks']};tasks=[]
    for result in read_json(RUN/'round_0/results.json')['tasks']:
        if result['status'] not in ('rejected','invalid_generation'):continue
        t=original[result['task_id']];name=t['task_id'].replace(':','__');view=read_json(t['view_ref'])
        selected=read_json(RUN/'round_0/generate'/f'{name}.json')
        feedback=(dict(kind='judge_feedback',judgment=read_json(RUN/'round_0/judge'/f'{name}.json'))
                  if result['status']=='rejected' else dict(kind='structure_errors',errors=result['errors']))
        dest=RUN/'round_1/repair_input'/f'{name}.json'
        write_json(dest,dict(view,previous_output=selected,revision_feedback=feedback))
        tasks.append(dict(t,repair_input_ref=str(dest),trigger=result['status']))
    write_json(RUN/'revision_plan.json',dict(max_external_revisions=1,tasks=tasks))
    print(json.dumps(dict(revision_tasks=len(tasks),task_ids=[t['task_id'] for t in tasks]),ensure_ascii=False))


def usage():
    registry=read_json(RUN/'agents.json');rows=[]
    for agent in registry['agents']:
        with sqlite3.connect('file:/home/bjx/.codex/state_5.sqlite?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            r=db.execute('select id,rollout_path,model,reasoning_effort,agent_path from threads where id=?',(agent['thread_id'],)).fetchone()
        if r is None:raise ValueError('agent_metadata_missing')
        assert r['model']=='gpt-5.6-luna' and r['reasoning_effort']=='high'
        events=[];contexts=[];responses={}
        for line in Path(r['rollout_path']).open():
            try:d=json.loads(line)
            except json.JSONDecodeError:continue
            payload=d.get('payload',{})
            if d.get('type')=='token_usage_record':
                response_id=payload['response_id']
                if response_id in responses:assert responses[response_id]==payload['usage']
                responses[response_id]=payload['usage']
            if d.get('type')=='turn_context':
                contexts.append(dict(model=payload.get('model'),effort=payload.get('effort')))
            if d.get('type')=='event_msg' and payload.get('type')=='token_count' and payload.get('info'):
                info=payload['info']
                if info.get('total_token_usage'):
                    events.append(dict(total=info['total_token_usage'],last=info.get('last_token_usage'),timestamp=d.get('timestamp')))
        assert events and all(c['model']=='gpt-5.6-luna' and c['effort']=='high' for c in contexts)
        total=events[-1]['total'];assert total['input_tokens']+total['output_tokens']==total['total_tokens']
        for key in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','total_tokens'):
            assert sum(r.get(key,0) for r in responses.values())==total.get(key,0)
        snapshots={json.dumps(e['total'],sort_keys=True):e for e in events}
        rows.append(dict(**agent,model=r['model'],reasoning_effort=r['reasoning_effort'],actual_usage=total,
            noncached_input_tokens=total['input_tokens']-total.get('cached_input_tokens',0),
            observed_usage_snapshots=len(snapshots),
            model_response_count=len(responses),per_response_sum_matches_cumulative=True,
            max_single_reported_total=max(e['last']['total_tokens'] for e in events if e.get('last')),
            max_single_reported_input=max(e['last']['input_tokens'] for e in events if e.get('last')),
            usage_evidence=events,rollout_ref=r['rollout_path'],read_at_last_event=events[-1]['timestamp']))
    keys=('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','total_tokens')
    totals={k:sum(r['actual_usage'].get(k,0) for r in rows) for k in keys}
    result=dict(agents=rows,totals=totals,noncached_input_tokens=totals['input_tokens']-totals['cached_input_tokens'],
        source='Codex rollout token_count totals; last cumulative snapshot once per unique agent thread',
        cache_is_subset_of_input=True,reasoning_is_subset_of_output=True,main_agent_excluded=True,
        model_api_pricing_not_available=True,actual_currency_cost_unknown=True,
        max_single_reported_total=max(r['max_single_reported_total'] for r in rows))
    assert len({a['thread_id'] for a in rows})==len(rows)
    write_json(RUN/'usage.json',result)
    print(json.dumps(dict(totals=totals,noncached_input_tokens=result['noncached_input_tokens'],
                         max_single_reported_total=result['max_single_reported_total']),ensure_ascii=False))


def report():
    m=read_json(RUN/'manifest.json');first=read_json(RUN/'round_0/results.json');u=read_json(RUN/'usage.json')
    second=read_json(RUN/'round_1/results.json') if (RUN/'round_1/results.json').exists() else dict(tasks=[])
    after={t['task_id']:t for t in second['tasks']};rows=[];reading=['# Luna 5.6通过版rubric','']
    for a in first['tasks']:
        n=1 if a['task_id'] in after else 0;last=after.get(a['task_id'],a)
        row=dict(task_id=a['task_id'],first_status=a['status'],final_status=last['status'],final_round=n,
                 first_scores=a.get('scores',{}),final_scores=last.get('scores',{}),task_retained=True)
        if last['status']=='passed':
            name=a['task_id'].replace(':','__');rubric=read_json(RUN/f'round_{n}'/'compiled'/f'{name}.json')
            write_json(RUN/'accepted'/f'{name}.json',rubric)
            reading+=['## '+a['task_id'],'',f'通过轮次：{n}，评分：'+json.dumps(row['final_scores'],ensure_ascii=False),'',
                      '```json',json.dumps(rubric,ensure_ascii=False,indent=2),'```','']
        rows.append(row)
    initial=sum(r['first_status']=='passed' for r in rows);final=sum(r['final_status']=='passed' for r in rows)
    result=dict(tasks=rows,first_pass=initial,final_pass=final,first_pass_rate=initial/10,final_pass_rate=final/10,
        total_tokens=u['totals']['total_tokens'],tokens_per_passed_rubric=u['totals']['total_tokens']/final if final else None,
        max_external_revisions=1,static_review_is_not_a_gate=True,original_tasks_retained=10)
    write_json(RUN/'results.json',result)
    text=['# gpt-5.6-luna high 子agent同10题试跑','',
        f'首次通过{initial}/10（{initial/10:.0%}），最多一轮修订后通过{final}/10（{final/10:.0%}）。', '',
        '通过条件沿用：原文/结构检查通过，且五个有效整数评分均非0。1分可通过。独立静态复核不是额外门槛。'
        '原题全部保留。生成与评分分别使用独立子agent，修订后的审查另开新子agent；每个子agent处理一批题，'
        '并非每题单独一个子agent。审查输入只含题面、公开来源与当前rubric，不含历史评分或修订意见。','',
        '## 真实token用量','',
        '| 子agent职责 | 输入（含缓存） | 其中缓存输入 | 输出（含思考） | 合计 |',
        '|---|---:|---:|---:|---:|']
    for a in u['agents']:
        x=a['actual_usage'];text.append(f"| {a['role']} | {x['input_tokens']:,} | {x['cached_input_tokens']:,} | {x['output_tokens']:,} | {x['total_tokens']:,} |")
    x=u['totals'];text+= [f"| 总计 | {x['input_tokens']:,} | {x['cached_input_tokens']:,} | {x['output_tokens']:,} | {x['total_tokens']:,} |",'',
        f"非缓存输入{u['noncached_input_tokens']:,}；思考输出子项{x['reasoning_output_tokens']:,}，已包含在输出内，不另加。"
        '用量来自各子agent运行记录中的最后累计token_count，未对累计快照重复求和，不包含主agent协调用量。'
        '同时以唯一response_id的token_usage_record逐次相加核对，结果与累计值一致。'
        '这是多次模型请求累计值，包含重复传入的上下文；不能理解为单次上下文长度。','',
        f"四个职责分别由独立子agent执行，共{sum(a['model_response_count'] for a in u['agents'])}次模型响应记录。"
        '初稿与首次审查处理10题，修订与复审只处理4题；一轮是一次外部评分反馈后的修订，不是整个agent只调用模型一次。','',
        f"观察到的最大单次输入+输出为{u['max_single_reported_total']:,}。spawn工具没有暴露硬上下文上限，"
        '128k通过子agent指令和事后实际用量核验；不是API版的发送前硬限制。'
        '没有可验证的该子agent人民币单价/账单，因此不换算人民币费用。','',
        '## 逐题结果','', '| 题目 | 首次 | 最终 | 最终五维分数 |','|---|---|---|---|']
    dims=('source_support','coverage','requirement_classification','evidence_design','decision_logic')
    for r in rows:text.append('| '+r['task_id']+' | '+r['first_status']+' | '+r['final_status']+' | '+'/'.join(str(r['final_scores'].get(k,'—')) for k in dims)+' |')
    text+=['','## 一轮修订后仍未通过的原因','']
    for r in rows:
        if r['final_status']=='passed':continue
        f=RUN/f"round_{r['final_round']}"/'judge'/f"{r['task_id'].replace(':','__')}.json"
        if f.exists():
            for dim in read_json(f)['dimensions']:
                if dim['score']==0:text.append(f"- {r['task_id']} / {dim['name']}：审查员认为{dim['reason']}")
    text+=['','## 对比边界','',
        '上一轮Qwen API流程首次3/10、最多两轮后5/10，用量338673 tokens；本轮使用工具型子agent、high推理、最多一轮修订。'
        '子agent可在单次任务内做离线结构自检，系统与工具上下文也不同，因此不是同一输入/同一调用机制的模型对照。'
        '通过率是该流程审查标准的通过率，不是独立标注或真实环境中的正确率。','']
    (RUN/'report_zh.md').write_text('\n'.join(text));(RUN/'accepted_rubrics_zh.md').write_text('\n'.join(reading))
    print(json.dumps(dict(first_pass=initial,final_pass=final,total_tokens=x['total_tokens']),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['compile','review','plan','usage','report']);p.add_argument('--round',type=int,choices=[0,1],default=0);a=p.parse_args()
    if a.action=='compile':compile_round(a.round)
    elif a.action=='review':review_round(a.round)
    elif a.action=='plan':revision_plan()
    elif a.action=='usage':usage()
    else:report()
