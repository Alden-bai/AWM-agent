"""Source-bound Qwen compilation and fresh-context DeepSeek five-axis review.

Ten original tasks, at most ten calls per provider. No retry, repair or task
deletion. The model's intermediate output and deterministic final rubric are
separate, explicitly named artifacts.
"""
import argparse
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import rubric_pilot as pilot
from rubric_pilot import APIConfig, Budget, api_request, file_hash, read_json, write_json
from rubric_qwen_pilot import LEDGER, identity, ledger_snapshot
from rubric_qwen_v11 import freeze, parse_response
from rubric_candidates import prepare_input, validate_selection, compile_rubric, evaluate_scores, DIMENSIONS
from harness_awm.protocol import estimate_tokens
from harness_awm.qwen import load_qwen_config

ROOT=pilot.ROOT
RUN=ROOT/'runs/rubric_qwen_deepseek_v12_10_20261024'
BASE=ROOT/'runs/rubric_qwen_v11_10_20261024'
THINKING=16384
CAP=450000
STAGES=('qwen','deepseek')
PROMPTS=dict(qwen=ROOT/'configs/rubric/select_compile_v12.txt',deepseek=ROOT/'configs/rubric/judge_deepseek_v12.txt')


def configs():
    qwen=load_qwen_config().actor_api.model_copy(update={'temperature':0,'timeout':600,'enable_thinking':True})
    values=dotenv_values('/home/bjx/app-agent/.env',interpolate=False)
    if values.get('DEEPSEEK_BASE_URL','').rstrip('/')!='https://api.deepseek.com':
        raise ValueError('unexpected_deepseek_destination')
    if values.get('DEEPSEEK_MODEL')!='deepseek-flash':
        raise ValueError('unexpected_deepseek_model')
    key=values.get('DEEPSEEK_API_KEY')
    if not key: raise ValueError('missing_deepseek_credentials')
    os.environ['RUBRIC_V12_DEEPSEEK_KEY']=key
    deepseek=APIConfig(base_url='https://api.deepseek.com',model='deepseek-flash',
        api_key_env='RUBRIC_V12_DEEPSEEK_KEY',timeout=600,context_limit=128000)
    assert qwen.model=='qwen3.5-flash'
    return dict(qwen=qwen,deepseek=deepseek)


def request(stage,payload,api,prompt):
    req=dict(model=api.model,messages=[dict(role='system',content=prompt),
        dict(role='user',content=json.dumps(payload,ensure_ascii=False,separators=(',',':')))],
        response_format={'type':'json_object'})
    if stage=='qwen':
        req.update(temperature=0,max_tokens=10000,extra_body={'enable_thinking':True,'thinking_budget':THINKING})
    else:
        req.update(max_tokens=16384,reasoning_effort='high',extra_body={'thinking':{'type':'enabled'}})
    return req


def reservation(stage,req):
    return estimate_tokens(req['messages'])+req['max_tokens']+(THINKING if stage=='qwen' else 0)+512


class PilotBudget(Budget):
    def reserve(self,key,stage,messages,output_tokens):
        return super().reserve(key,stage,messages,output_tokens+(THINKING if stage=='v12_qwen' else 0))


def prepare():
    if (RUN/'manifest.json').exists(): raise SystemExit('Already prepared; immutable artifacts retained')
    cfg=configs(); old=read_json(BASE/'manifest.json');tasks=[]
    for stage,p in PROMPTS.items():
        d=RUN/'prompts'/f'{stage}.txt';d.parent.mkdir(parents=True,exist_ok=True);d.write_text(p.read_text())
    for t in old['tasks']:
        assert file_hash(t['input_ref'])==t['input_sha256']
        inp=read_json(t['input_ref']);view,pack=prepare_input(inp)
        d=RUN/'inputs'/t['task_id'].replace(':','__')
        write_json(d/'original.json',inp);write_json(d/'candidate_input.json',view);write_json(d/'candidate_audit.json',pack)
        req=request('qwen',view,cfg['qwen'],PROMPTS['qwen'].read_text())
        upper=reservation('qwen',req)
        if upper>=128000: raise ValueError('qwen_context_guard_exceeded')
        tasks.append(dict(task_id=t['task_id'],input_ref=str(d/'original.json'),input_sha256=file_hash(d/'original.json'),
            view_ref=str(d/'candidate_input.json'),view_sha256=file_hash(d/'candidate_input.json'),
            audit_ref=str(d/'candidate_audit.json'),audit_sha256=file_hash(d/'candidate_audit.json'),
            previous_input_ref=t['input_ref'],previous_input_sha256=t['input_sha256'],candidates=len(pack['candidates']),
            qwen_reservation_upper=upper))
    before=ledger_snapshot()
    m=dict(version='source-bound-v12',tasks=tasks,apis={k:v.model_dump() for k,v in cfg.items()},
        budget_before=before,experiment_cap=CAP,cumulative_cap=before['tokens_accounted_or_reserved']+CAP,
        cap_provenance='operator protection ceiling for explicitly authorized ten-task pilot',
        max_requests=20,max_calls_per_stage=10,concurrency=2,automatic_retries=0,paid_repairs=0,full_run=False,
        request_prefix='rubric_v12',context_guard=128000,prompt_hashes={k:file_hash(RUN/'prompts'/f'{k}.txt') for k in STAGES},
        implementation_hashes={str(p):file_hash(p) for p in [Path(__file__),ROOT/'scripts/rubric_candidates.py']},
        baseline_refs={name:dict(path=str(ROOT/'runs'/name/'cost_report.json'),sha256=file_hash(ROOT/'runs'/name/'cost_report.json'))
            for name in ('rubric_qwen_v10_10_20261024','rubric_qwen_v10_thinking_10_20261024','rubric_qwen_v11_10_20261024')},
        review_reference_sha256=file_hash(LEDGER.parent/'review_reference.json'),
        evaluation=dict(dimensions=list(DIMENSIONS),zero_policy='reject_this_rubric_keep_original_task',
            one_policy='review',all_two_policy='eligible_candidate_not_runtime_certification',invalid_judge='review',
            fresh_context=True,no_generation_reasoning_or_prior_scores=True),
        pricing=dict(checked_date='2026-10-24',currency='CNY',
            qwen=dict(input_per_million=0.2,output_per_million=2.0,source='https://help.aliyun.com/zh/model-studio/qwen3-5-flash'),
            deepseek=dict(off_peak=dict(hit=0.02,miss=1.0,output=4.0),peak=dict(hit=0.04,miss=2.0,output=8.0),
                source='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                rate_time_rule='Beijing weekday 09-12/14-18 peak excluding public holidays; this pilot is on Saturday; report actual response timestamps')),
        comparison_boundary='Same known ten-task development set; deterministic assembly and cross-model judging introduced together; prior runs have no identical DeepSeek five-axis scores')
    write_json(RUN/'manifest.json',m)
    write_json(RUN/'authorization.json',dict(source='direct_user_message',
        text='那就按这个方案落地，代码提取候选约束并绑定原文，Qwen筛选、去重、区分硬要求和质量偏好，再生成五字段rubric。用新DeepSeekflash上下文的LLM按五个维度打0、1、2分，有0分就淘汰这版rubric，保留原题。还是用这10道题试跑，单次不超过128k，对比质量和费用。',
        approved_scope='Same ten public synthetic tasks; Qwen compilation and DeepSeek Flash fresh-context judge; keep every original task',
        manifest_sha256=file_hash(RUN/'manifest.json')))
    provenance=read_json(BASE/'public_data_provenance.json')
    provenance.update(destinations={k:v.base_url for k,v in cfg.items()},max_requests=20,operator_token_cap=CAP,
        new_destination_explicitly_authorized='DeepSeek Flash named in current direct user instruction',
        input_refs_match_previous_frozen_public_sources=True)
    write_json(RUN/'public_data_provenance.json',provenance)
    print(json.dumps(dict(prepared=10,candidates=sum(t['candidates'] for t in tasks),max_qwen_guard=max(t['qwen_reservation_upper'] for t in tasks),
                         budget_before=before,max_calls=20,operator_token_cap=CAP),ensure_ascii=False))


async def run():
    m=read_json(RUN/'manifest.json');cfg=configs()
    assert read_json(RUN/'authorization.json')['manifest_sha256']==file_hash(RUN/'manifest.json')
    assert {k:v.model_dump() for k,v in cfg.items()}==m['apis']
    for p,h in m['implementation_hashes'].items(): assert file_hash(p)==h
    for s,h in m['prompt_hashes'].items(): assert file_hash(RUN/'prompts'/f'{s}.txt')==h
    budget=PilotBudget(LEDGER,m['budget_before']['physical_requests']+m['max_requests'],m['cumulative_cap'])
    sem=asyncio.Semaphore(m['concurrency'])

    async def call(stage,t,payload,deps):
        d=RUN/stage/t['task_id'].replace(':','__')
        req=request(stage,payload,cfg[stage],(RUN/'prompts'/f'{stage}.txt').read_text())
        upper=reservation(stage,req)
        if upper>=128000: raise ValueError('context_guard_exceeded')
        freeze(d/'planned_request.json',req)
        sig=identity(req,m['apis'][stage],t['input_sha256'])
        key=m['request_prefix']+'/'+stage+'/'+t['task_id']+'/'+sig
        freeze(d/'request_identity.json',dict(signature=sig,request_key=key,request_sha256=file_hash(d/'planned_request.json'),
            reservation_upper=upper,dependencies=deps))
        if (d/'response.json').exists(): data=read_json(d/'response.json')
        else:
            with sqlite3.connect(LEDGER) as db:
                if db.execute('select 1 from requests where request_key=?',(key,)).fetchone(): raise ValueError('unknown_request_no_retry')
            async with sem:
                data=await api_request(cfg[stage],req,budget,key,'v12_'+stage,d)
            print(json.dumps(dict(task_id=t['task_id'],stage=stage,finish=data['choices'][0]['finish_reason'],usage=data.get('usage')),ensure_ascii=False),flush=True)
        (d/'raw_content.txt').write_text(data['choices'][0]['message'].get('content') or '')
        out=parse_response(data);freeze(d/'output.json',out)
        return out

    async def one(t):
        name=t['task_id'].replace(':','__');state=dict(task_id=t['task_id'],stage='qwen',status='running',task_retained=True)
        try:
            for key,h in [('input_ref','input_sha256'),('view_ref','view_sha256'),('audit_ref','audit_sha256')]:
                assert file_hash(t[key])==t[h]
            inp,view,pack=read_json(t['input_ref']),read_json(t['view_ref']),read_json(t['audit_ref'])
            selected=await call('qwen',t,view,dict(view=t['view_sha256'],candidate_audit=t['audit_sha256']))
            errors=validate_selection(selected,inp,pack)
            write_json(RUN/'qwen'/name/'validation.json',dict(errors=errors))
            if errors:
                state.update(status='invalid_generation',errors=errors);return
            rubric=compile_rubric(selected,inp,pack)
            freeze(RUN/'compiled'/name/'rubric.json',rubric)
            freeze(RUN/'compiled'/name/'provenance.json',dict(assembly='deterministic_source_binding_and_decision_wrapping',
                raw_selection_sha256=file_hash(RUN/'qwen'/name/'output.json'),candidate_audit_sha256=t['audit_sha256'],
                rubric_sha256=file_hash(RUN/'compiled'/name/'rubric.json'),no_semantic_repair=True))
            state['stage']='deepseek'
            # No previous scores, old reviews, raw response or reasoning_content.
            payload=dict(view,selection=selected['selection'],candidate_rubric=rubric)
            judgment=await call('deepseek',t,payload,dict(rubric=file_hash(RUN/'compiled'/name/'rubric.json'),
                selection=file_hash(RUN/'qwen'/name/'output.json')))
            result=evaluate_scores(judgment,t['task_id'],{i['id'] for i in rubric['rubric']},{c['candidate_id'] for c in pack['candidates']})
            write_json(RUN/'deepseek'/name/'evaluation.json',result)
            state.update(status=result['status'],scores=result['scores'],errors=result['errors'],zero_dimensions=result['zero_dimensions'])
        except Exception as exc:
            state.update(status='stopped',error_type=type(exc).__name__)
        finally:
            write_json(RUN/'task_status'/f'{name}.json',state)
            print(json.dumps(state,ensure_ascii=False),flush=True)
    await asyncio.gather(*(one(t) for t in m['tasks']))
    print(json.dumps(budget.summary()),flush=True)
    summarize()


def cost(stage,usage,created,prices):
    if stage=='qwen':
        r=prices['qwen']
        return dict(estimated_cny=(usage['prompt_tokens']*r['input_per_million']+usage['completion_tokens']*r['output_per_million'])/1e6,
                    rate_band='Beijing input <=128k',cache_accounting='ordinary input price; no unverified discount')
    dt=datetime.fromtimestamp(created,timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    peak=dt.weekday()<5 and (9<=dt.hour<12 or 14<=dt.hour<18)
    # A weekday public-holiday lookup is intentionally not guessed.
    if peak: raise ValueError('weekday_peak_needs_holiday_verification')
    r=prices['deepseek']['off_peak']
    hit=usage.get('prompt_cache_hit_tokens',(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0))
    miss=usage.get('prompt_cache_miss_tokens',usage['prompt_tokens']-hit)
    if hit+miss!=usage['prompt_tokens']: raise ValueError('cache_usage_mismatch')
    return dict(estimated_cny=(hit*r['hit']+miss*r['miss']+usage['completion_tokens']*r['output'])/1e6,
        rate_band='off_peak',timestamp_beijing=dt.isoformat(),cache_hit_tokens=hit,cache_miss_tokens=miss)


def summarize():
    m=read_json(RUN/'manifest.json')
    with sqlite3.connect(f'file:{LEDGER}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;ledger=[dict(x) for x in db.execute('select * from requests')]
    own=[r for r in ledger if r['request_key'].startswith(m['request_prefix']+'/')]
    requests=[];rows=[];reading=['# v12 五字段rubric与DeepSeek五维评分','',
        '来源与固定判定由程序装配，业务内容来自Qwen；原始中间输出另存。淘汰仅针对rubric，原题保留。','']
    for t in m['tasks']:
        name=t['task_id'].replace(':','__');inp=read_json(t['input_ref']);view,pack=prepare_input(inp)
        assert view==read_json(t['view_ref']) and pack==read_json(t['audit_ref'])
        assert file_hash(t['input_ref'])==t['input_sha256']
        row=dict(task_id=t['task_id'],compiled=False,task_retained=True)
        selected=None;rubric=None
        for stage in STAGES:
            d=RUN/stage/name
            if not (d/'response.json').exists():continue
            req=read_json(d/'request.json');planned=read_json(d/'planned_request.json');rid=read_json(d/'request_identity.json')
            assert req==planned and file_hash(d/'planned_request.json')==rid['request_sha256']
            assert identity(req,m['apis'][stage],t['input_sha256'])==rid['signature']
            assert reservation(stage,req)==rid['reservation_upper']<128000
            assert req['messages'][0]['content']==(RUN/'prompts'/f'{stage}.txt').read_text()
            payload=json.loads(req['messages'][1]['content'])
            if stage=='qwen':assert payload==view
            else:assert payload==dict(view,selection=selected['selection'],candidate_rubric=rubric)
            response=read_json(d/'response.json');usage=response['usage']
            record=[r for r in own if r['request_key']==rid['request_key']]
            assert len(record)==1 and record[0]['status']=='response' and json.loads(record[0]['usage'])==usage
            assert usage['total_tokens']==usage['prompt_tokens']+usage['completion_tokens']==record[0]['reserved']
            assert usage['total_tokens']<=rid['reservation_upper']
            requests.append(dict(task_id=t['task_id'],stage=stage,model=response.get('model'),finish_reason=response['choices'][0]['finish_reason'],
                **{k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')},
                reasoning_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0),
                reservation_upper=rid['reservation_upper'],**cost(stage,usage,response['created'],m['pricing'])))
            if (d/'output.json').exists():
                parsed=parse_response(response);assert parsed==read_json(d/'output.json')
                if stage=='qwen':
                    selected=parsed;errors=validate_selection(selected,inp,pack);row['selection_errors']=errors
                    if not errors:
                        rubric=compile_rubric(selected,inp,pack)
                        assert rubric==read_json(RUN/'compiled'/name/'rubric.json')
                        row.update(compiled=True,required=sum(i['requirement_level']=='required' for i in rubric['rubric']),
                            quality=sum(i['requirement_level']=='quality' for i in rubric['rubric']))
                else:row['judge']=evaluate_scores(parsed,t['task_id'],{i['id'] for i in rubric['rubric']},{c['candidate_id'] for c in pack['candidates']})
        state=RUN/'task_status'/f'{name}.json';row['state']=read_json(state) if state.exists() else dict(status='pending')
        reading+=['## '+t['task_id'],'',inp['task'],'',f"状态：{row['state']['status']}；原题保留。",'']
        if row.get('judge'):reading += ['五维评分：'+json.dumps(row['judge']['scores'],ensure_ascii=False),'']
        if rubric: reading+=['```json',json.dumps(rubric,ensure_ascii=False,indent=2),'```','']
        else:reading+=['本题没有可装配的完整rubric；保留原始中间输出与失败原因。','']
        rows.append(row)
    metrics=('prompt_tokens','completion_tokens','total_tokens','reasoning_tokens','estimated_cny')
    totals={k:sum(r[k] for r in requests) for k in metrics}
    stages={s:{k:sum(r[k] for r in requests if r['stage']==s) for k in metrics} for s in STAGES}
    summary=dict(tasks=10,compiled=sum(r['compiled'] for r in rows),judge_completed=sum('judge' in r for r in rows),
        eligible=sum(r.get('judge',{}).get('status')=='eligible' for r in rows),
        rejected=sum(r.get('judge',{}).get('status')=='rejected' for r in rows),
        review=sum(r.get('judge',{}).get('status')=='review' for r in rows),original_tasks_retained=10,runtime_accuracy_measured=False)
    report=dict(summary=summary,totals=totals,stages=stages,tasks=rows,requests=requests,physical_requests=len(own),
        unknown_reserved_tokens=sum(r['reserved'] for r in own if r['status']!='response'),
        cumulative_confirmed_tokens=sum(r['reserved'] for r in ledger if r['status']=='response'),
        cumulative_unknown_reserved=sum(r['reserved'] for r in ledger if r['status']!='response'),
        max_actual_input_tokens=max((r['prompt_tokens'] for r in requests),default=0),
        max_conservative_reservation=max((r['reservation_upper'] for r in requests),default=0),
        experiment_cap=m['experiment_cap'],pricing=m['pricing'],actual_bill_verified=False,codex_development_review_usage_included=False)
    assert totals['total_tokens']+report['unknown_reserved_tokens']<=m['experiment_cap']
    write_json(RUN/'cost_report.json',report);write_json(RUN/'status.json',summary)
    # A retention manifest, never a deletion or modification of the dataset.
    write_json(RUN/'retention_manifest.json',dict(original_tasks_retained=True,
        rubric_versions=[dict(task_id=r['task_id'],status=r.get('judge',{}).get('status',r['state']['status']),
            zero_dimensions=r.get('judge',{}).get('zero_dimensions',[]),original_ref=next(t['input_ref'] for t in m['tasks'] if t['task_id']==r['task_id'])) for r in rows]))
    (RUN/'rubrics_zh.md').write_text('\n'.join(reading))
    print(json.dumps(dict(totals=totals,summary=summary),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run','summarize']);a=p.parse_args().action
    if a=='prepare':prepare()
    elif a=='run':asyncio.run(run())
    else:summarize()
