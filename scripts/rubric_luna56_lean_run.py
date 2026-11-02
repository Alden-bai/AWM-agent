"""Offline preparation and accounting for the authorized single-task agent pilot."""
import argparse
import json
import sqlite3
from pathlib import Path

import rubric_luna56_agents as base
from prepare_rubric_agent_prompts import build_pack
from rubric_pilot import ROOT, read_json, write_json, file_hash

RUN = ROOT / 'runs/rubric_luna56_lean_10_20261024'
BASE = ROOT / 'runs/rubric_luna56_agents_10_20261024'
base.RUN = RUN


def pack(role, data, n):
    name = data['task_id'].replace(':', '__')
    path = RUN / f'round_{n}' / ('judge' if role == 'judge' else 'generate') / f'{name}.json'
    payload = build_pack(role, data, path)
    payload['execution'] = payload['execution'].replace(
        '128k限制需由调度端检查输入、输出预留及实际用量；不要声称本prompt能保证token上限。',
        '本轮用户不设人为的单次上下文上限，仍需准确报告实际用量。')
    dest = RUN / f'round_{n}' / 'jobs' / role / f'{name}.json'
    if dest.exists():
        assert read_json(dest) == payload
    else:
        write_json(dest, payload)
    return str(dest)


def init():
    if RUN.exists():
        raise FileExistsError(RUN)
    m = read_json(BASE / 'manifest.json')
    m = {k: m[k] for k in ('tasks', 'model', 'reasoning_effort')}
    m.update(context_target=None, user_context_cap=None, max_external_revisions=1,
             one_task_per_context=True, fork_turns='none', fresh_judge_each_task_and_round=True,
             pass_rule='valid_structure_and_five_valid_scores_without_zero', baseline_ref=str(BASE),
             root_coordination_usage_excluded=True, original_tasks_retained=True)
    for t in m['tasks']:
        assert file_hash(t['view_ref']) == t['view_sha256']
        assert file_hash(t['input_ref']) == t['input_sha256']
    write_json(RUN / 'manifest.json', m)
    for t in m['tasks']:
        pack('generate', read_json(t['view_ref']), 0)
    print(json.dumps(dict(prepared=len(m['tasks']), run=str(RUN))))


def compile_round(n):
    base.compile_round(n)
    for t in read_json(RUN / f'round_{n}/compilation.json')['tasks']:
        if not t['errors']:
            name=t['task_id'].replace(':','__')
            pack('judge', read_json(RUN / f'round_{n}/judge_input/{name}.json'), n)


def plan():
    base.revision_plan()
    for t in read_json(RUN / 'revision_plan.json')['tasks']:
        pack('revise', read_json(t['repair_input_ref']), 1)


def registry():
    rows=[]
    with sqlite3.connect('file:/home/bjx/.codex/state_5.sqlite?mode=ro', uri=True) as db:
        for role, prefix in [('initial_generation','lean10g'), ('initial_judge','lean10j'),
                             ('one_revision_generation','lean10r'), ('fresh_final_judge','lean10f')]:
            for tid, path in db.execute('select id,agent_path from threads where agent_path like ?',('/root/'+prefix+'%',)):
                rows.append(dict(role=role, task_path=path, thread_id=tid))
    write_json(RUN/'agents.json',dict(agents=rows))
    print(json.dumps(dict(agents=len(rows))))


def report():
    m=read_json(RUN/'manifest.json'); u=read_json(RUN/'usage.json')
    first=read_json(RUN/'round_0/results.json')['tasks']
    second={t['task_id']:t for t in read_json(RUN/'round_1/results.json')['tasks']} if (RUN/'round_1/results.json').exists() else {}
    rows=[]; accepted=['# 通过版 rubric','']
    for a in first:
        n=int(a['task_id'] in second); b=second.get(a['task_id'],a); name=a['task_id'].replace(':','__')
        rows.append(dict(task_id=a['task_id'], first_status=a['status'], final_status=b['status'],
                         final_round=n, first_scores=a.get('scores',{}), final_scores=b.get('scores',{})))
        if b['status']=='passed':
            rubric=read_json(RUN/f'round_{n}/compiled/{name}.json')
            write_json(RUN/f'accepted/{name}.json',rubric)
            accepted.extend(['## '+a['task_id'],'','```json',json.dumps(rubric,ensure_ascii=False,indent=2),'```',''])
    initial=sum(r['first_status']=='passed' for r in rows); final=sum(r['final_status']=='passed' for r in rows)
    prev=read_json(BASE/'usage.json'); old=prev['totals']; new=u['totals']
    result=dict(tasks=rows,first_passed=initial,final_passed=final,total_tasks=10,
                first_pass_rate=initial/10,final_pass_rate=final/10,revision_tasks=len(second),
                total_tokens=new['total_tokens'],token_reduction=1-new['total_tokens']/old['total_tokens'],
                tokens_per_passed_rubric=new['total_tokens']/final if final else None,
                original_tasks_retained=10)
    write_json(RUN/'results.json',result)
    txt=['# Luna high 节省 token prompt 重跑','',
         '同样10题，gpt-5.6-luna/high；每题每阶段新agent，审查不含历史评分或修订反馈，最多一轮修订。原生成与审查业务prompt原样保留，新增执行约束；本轮未设人为单次上下文上限。','',
         '| 指标 | 上轮 | 本轮 |','|---|---:|---:|',f'| 首次通过 | 6/10（60%） | {initial}/10（{initial*10}%） |',
         f'| 最终通过 | 9/10（90%） | {final}/10（{final*10}%） |']
    for label,key in [('总token','total_tokens'),('输入（含缓存）','input_tokens'),('缓存输入','cached_input_tokens'),('输出（含思考）','output_tokens'),('思考（输出子项）','reasoning_output_tokens')]:
        txt.append(f'| {label} | {old[key]:,} | {new[key]:,} |')
    txt.extend([f'| 非缓存输入 | {prev["noncached_input_tokens"]:,} | {u["noncached_input_tokens"]:,} |',
                f'| 模型响应次数 | 70 | {sum(a["model_response_count"] for a in u["agents"])} |','',
                f'总token减少 {result["token_reduction"]:.2%}。全部试跑token均计入，含失败与修订；不含主agent协调用量。缓存属于输入、思考属于输出，均不重复相加。未核实人民币计费，不能把token比例直接当作费用比例。','',
                '## 逐题结果','','| 题目 | 首次 | 最终 | 最终五维分数 |','|---|---|---|---|'])
    dims=('source_support','coverage','requirement_classification','evidence_design','decision_logic')
    for r in rows:
        txt.append(f'| {r["task_id"]} | {r["first_status"]} | {r["final_status"]} | '+ '/'.join(str(r['final_scores'].get(d,'—')) for d in dims)+' |')
    txt.extend(['','## 未通过原因',''])
    for r in rows:
        if r['final_status']=='passed':
            continue
        last=second.get(r['task_id'],next(a for a in first if a['task_id']==r['task_id']))
        if last.get('errors'):
            txt.append(f'- {r["task_id"]}：'+', '.join(sorted(set(last['errors'])))+'；修订后仍将契约引用ID用作证据source_id，原题保留。')
        else:
            name=r['task_id'].replace(':','__')
            judgment=read_json(RUN/f'round_{r["final_round"]}/judge/{name}.json')
            for d in judgment['dimensions']:
                if d['score']==0:
                    txt.append(f'- {r["task_id"]} / {d["name"]}：审查员认为'+d['reason'])
    txt.extend(['','## 分阶段用量','','| 阶段 | agent数 | 模型响应数 | 总token |','|---|---:|---:|---:|'])
    for role in ('initial_generation','initial_judge','one_revision_generation','fresh_final_judge'):
        agents=[a for a in u['agents'] if a['role']==role]
        txt.append(f'| {role} | {len(agents)} | {sum(a["model_response_count"] for a in agents)} | {sum(a["actual_usage"]["total_tokens"] for a in agents):,} |')
    txt.extend(['','## 核验与边界','',
                '来源和题目哈希保持不变；每份初稿与修订稿由代码校验候选映射及原文引用。合法五维评分无0即通过，格式无效不算通过。没有增加第三道静态语义门槛。',
                'token来自每个唯一子agent的最终累计usage，并与按response_id去重后的逐响应usage求和核对。模型与high设置由本地线程元数据和turn_context核验。',
                '上轮按角色批量处理多题，本轮逐题独立，因此本对比同时包含执行prompt和上下文组织方式变化；不能把变化全部归因于某一句prompt。10题通过率是本流程审查结果，不是独立标注准确率。',
                '部分agent仍额外读取技能文件，prompt未硬性保证每个agent只有一次读与一次写。这些额外调用全部计入。总token下降，但非缓存输入增加、最终通过题数减少，不能据此宣称质量保持或费用按同比例下降。',''])
    (RUN/'report_zh.md').write_text('\n'.join(txt)); (RUN/'accepted_rubrics_zh.md').write_text('\n'.join(accepted))
    verified=[]
    for t in m['tasks']:
        assert file_hash(t['input_ref'])==t['input_sha256'] and file_hash(t['view_ref'])==t['view_sha256']
        verified.append(dict(task_id=t['task_id'],input_ref=t['input_ref'],unchanged=True))
    write_json(RUN/'retention_manifest.json',dict(tasks=verified))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['init','compile','review','plan','registry','usage','report']);p.add_argument('--round',type=int,choices=[0,1],default=0);a=p.parse_args()
    if a.action=='init':init()
    elif a.action=='compile':compile_round(a.round)
    elif a.action=='review':base.review_round(a.round)
    elif a.action=='plan':plan()
    elif a.action=='registry':registry()
    elif a.action=='usage':base.usage()
    else:report()
