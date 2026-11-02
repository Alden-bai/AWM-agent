#!/usr/bin/env python3
"""Frozen original-100 two-layer cleaning. Local evidence, bounded model requests."""
from __future__ import annotations
import argparse, asyncio, collections, copy, json, os, platform, sqlite3, subprocess, sys, time, traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import *
import prompts
sys.path.insert(0,str(ROOT/'scripts'))
import clean_data


def compact_contract(spec):
    def clean(x,in_response=False):
        if isinstance(x,dict):
            result={}
            for k,v in x.items():
                if k in ('example','examples','tags','required_fields'):continue
                if in_response and k=='description' and not re.search(r'inclus|exclus|current|always|default|time|UTC|zone|sort|unique|total|days|hours|limit|pag|owner|only|sum|count|averag|mean|percent',str(v),re.I):continue
                result[k]=clean(v,in_response or k=='response')
            return result
        if isinstance(x,list):return [clean(v,in_response) for v in x]
        return x
    return clean(spec)


def prepare(run):
    run=Path(run).resolve()
    if (run/'manifest.json').exists():return read_json(run/'manifest.json')
    old=read_json(ROOT/'evidence/legacy/cleaning/manifest.json')
    mapping={t['ledger_id']:t for t in old['tasks']};tasks=[mapping[i] for i in old['pilot100']]
    assert len(tasks)==100 and len({t['ledger_id'] for t in tasks})==100
    selected={t['scenario'] for t in tasks};assets={};inventory={}
    for name in clean_data.FILES:
        p=ROOT/'data/awm-dde80a0'/name;h=clean_data.sha(p)
        assert h==old['files'][name]['sha256'],name
        assets[name]=[r for r in clean_data.rows(p) if clean_data.normalize(r.get('scenario','')) in selected]
        inventory[name]={'sha256':h,'path':str(p),'selected_records':len(assets[name])}
    config=load_config('/home/bjx/app-agent/.env')
    m={'protocol':'two_layer_pilot_v1','created_at':now(),'tasks':tasks,'pilot100':old['pilot100'],
       'revision':old['revision'],'files':inventory,'source_manifest_sha256':digest(ROOT/'evidence/legacy/cleaning/manifest.json'),
       'judge':{**config.public(),'transport_retries':0,'temperature':1.0,'max_completion_tokens':4096,'effective_wire_max_tokens':4096,'mode':'sql_llm','parameter_alias':'max_completion_tokens -> max_tokens for configured DeepSeek endpoint; prompt/parser unchanged'},
       'builder':{'model':config.model,'temperature':0,'thinking':'disabled','max_tokens':16000},
       'budget':{'max_physical_requests':2500,'max_total_tokens':8000000,'stage_requests':{'build':360,'extract':180,'review':270,'judge':2200,'dispute':100},'repair_rounds_per_stage':2,'transport_retries':0,'judge_slots_per_case':5,'early_stop_confirmed_defect':True},
       'clock':{'timezone':'Asia/Shanghai','policy':'actual wall clock recorded, no fake-time monkeypatch; unresolved relative-time semantics -> review','started':now()},
       'source':{'python':sys.version,'platform':platform.platform(),'official_verify_sha256':digest(ROOT/'agent-world-model/awm/core/verify.py'),'official_server_sha256':digest(ROOT/'agent-world-model/awm/core/server.py'),'plan_sha256':digest(ROOT/'docs/two_layer_cleaning_plan_zh.md'),'implementation':{p.name:digest(p) for p in Path(__file__).parent.glob('*.py')}},
       'preflight':read_json(ROOT/'evidence/two_layer_preflight.json') if (ROOT/'evidence/two_layer_preflight.json').exists() else None}
    save_json(run/'selected_assets.json',assets);save_json(run/'manifest.json',m)
    Budget(run)
    return m


async def environments(run):
    run=Path(run);m=read_json(run/'manifest.json');assets=configure_runtime(run)
    checks=[]
    for name in dict.fromkeys(t['scenario'] for t in m['tasks']):
        folder=run/'environments'/name;folder.mkdir(parents=True,exist_ok=True)
        if (folder/'result.json').exists():checks.append(read_json(folder/'result.json'));continue
        def entry(f):
            rows=[r for r in assets[f] if r['scenario']==name]
            if len(rows)!=1:raise ValueError('Conflicting asset '+f)
            return rows[0]
        result=clean_data.check_environment(entry('gen_db.jsonl'),entry('gen_sample.jsonl'),entry('gen_envs.jsonl'),folder)
        result.update(scenario=name,checked_at=now())
        if result['initialization_complete']:
            second=clean_data.initialize(entry('gen_db.jsonl')['db_schema'],entry('gen_sample.jsonl')['sample_data'],folder/'reset.db')
            result['second_initialization']=second;result['reset_semantically_equal']=full_state(folder/'initial.db')==full_state(folder/'reset.db')
            task={'scenario':name,'task_idx':None,'task':'Discover public tools.'}
            trace=await runtime.execute_case(run,task,{'name':'discovery','calls':[],'answer':'','expected':True},folder/'initial.db',folder/'discovery')
            result['runtime_failure']=trace['failure']
            if not trace['failure']:
                tools=read_json(folder/'discovery/tools.json');result['tool_count']=len(tools);result['runtime_discovery_passed']=True
                import jsonschema
                errors=[]
                for tool in tools:
                    try:jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])
                    except Exception as e:errors.append({'name':tool['name'],'error':str(e)})
                result['tool_schema_errors']=errors
        save_json(folder/'result.json',result);checks.append(result)
        print(json.dumps({'environment':name,'initialization':result['initialization_complete'],'hard_defect':result['hard_defect'],'tools':result.get('tool_count'),'runtime_failure':result.get('runtime_failure')},ensure_ascii=False),flush=True)
    write_rows(run/'environment_checks.jsonl',checks)



def review_case(c):
    # Only remove exact repetition of frozen SQL and discovery; raw records remain on disk.
    out={k:v for k,v in c.items() if k not in ('path','checks_a','checks_b','trajectory')}
    for key in ('checks_a','checks_b'):
        if key in c:out[key]={i:{k:v for k,v in check.items() if k not in ('sql','description')} for i,check in c[key].items()}
    out['trajectory']=[{k:v for k,v in row.items() if k not in ('started_at','finished_at')} for row in c.get('trajectory',[]) if not any(tc['name']=='list_tools' for tc in row.get('tool_calls',[]))]
    return out


def relevant_state(initial,spec,independent,cases):
    state=full_state(initial)
    text=json.dumps([spec,independent,[c.get('db_changes') for c in cases]],ensure_ascii=False)
    selected={k:v for k,v in state.items() if re.search(r'(?<![a-zA-Z_])'+re.escape(k)+r'(?![a-zA-Z_])',text)}
    return {'complete_rows_for_referenced_tables':selected,'other_table_counts':{k:len(v) for k,v in state.items() if k not in selected},'full_database_available_locally':str(initial)}


def differences(initial,final):
    out={}
    for table in sorted(set(initial)|set(final)):
        before=initial.get(table,[]);after=final.get(table,[])
        if before!=after:
            a={json.dumps(r,sort_keys=True,ensure_ascii=False) for r in before};b={json.dumps(r,sort_keys=True,ensure_ascii=False) for r in after}
            out[table]={'removed':[json.loads(x) for x in sorted(a-b)],'added':[json.loads(x) for x in sorted(b-a)]}
    return out


def coverage(spec,cases):
    matrix=[]
    for r in spec.get('requirements',[]):
        positives=[c['name'] for c in cases if c['expected'] and c.get('label_valid') and c['checks_a'][r['id']]['passed']]
        negatives=[c['name'] for c in cases if not c['expected'] and c.get('label_valid') and c.get('target')==r['id'] and not c['checks_a'][r['id']]['passed']]
        matrix.append({'requirement_id':r['id'],'source_quote':r['source_quote'],'positive':positives,'negative':negatives,'status':'covered' if positives and negatives else 'missing'})
    return matrix


async def execute_cases(run,task,spec,independent,folder):
    initial=Path(run)/'environments'/task['scenario']/'initial.db'
    base={**spec,'negatives':[]}
    recipes=make_cases(base);build_failures=[]
    for negative in spec.get('negatives',[]):
        try:recipes.append(make_cases({**spec,'equivalent_positive':None,'negatives':[negative]})[-1])
        except Exception as e:
            build_failures.append({'name':negative['id'],'expected':False,'target':negative.get('target'),'label_valid':False,'failure':{'type':'candidate_build_error','error':str(e)},'not_executed':True,'checks_a':{r['id']:{'valid':False,'passed':False,'error':'case_not_built'} for r in spec['requirements']},'checks_b':{},'tool_checks':[],'trajectory':[],'db_changes':{}})
    save_json(folder/'frozen_candidates.json',{'created_at':now(),'spec_sha256':digest(spec),'independent_sha256':digest(independent),'cases':recipes,'build_failures':build_failures,'before_judge':True})
    cases=list(build_failures)
    for recipe in recipes:
        where=folder/'cases'/recipe['name']
        if (where/'evaluated.json').exists():cases.append(read_json(where/'evaluated.json'));continue
        trace=await runtime.execute_case(run,task,recipe,initial,where)
        a=evaluate(spec['requirements'],where/'initial.db',where/'final.db',trace)
        b=evaluate(independent.get('requirements',[]),where/'initial.db',where/'final.db',trace)
        toolchecks=[{'step':x['step'],'description':x['description'],**query_check(x['sql'],where/'initial.db',where/'final.db',trace)} for x in spec.get('tool_checks',[])] if recipe['expected'] else []
        actual_errors=[x for x in trace['trajectory'] if x.get('is_error')]
        finite=bool(a) and all(x.get('valid') for x in a.values())
        correct=finite and all(x['passed'] for x in a.values())
        bcorrect=bool(b) and all(x.get('valid') and x['passed'] for x in b.values())
        # A verified negative must truly violate its declared target, with successful execution.
        label_valid=finite and not trace['failure'] and not actual_errors and (correct and bcorrect if recipe['expected'] else recipe['target'] in a and not a[recipe['target']]['passed'])
        c={k:recipe[k] for k in ('name','expected','target')};c.update(path=str(where),failure=trace['failure'],tool_errors=actual_errors,checks_a=a,checks_b=b,tool_checks=toolchecks,label_valid=bool(label_valid),correct_a=correct,correct_b=bcorrect,violates=[k for k,v in a.items() if v.get('valid') and not v['passed']],trajectory=trace['trajectory'],final_answer=trace['final_answer'],db_changes=differences(full_state(where/'initial.db'),full_state(where/'final.db')))
        save_json(where/'evaluated.json',c);cases.append(c)
    # Independent fresh replay of the entire positive, checking semantics not timestamp bytes.
    replay=folder/'positive_replay'
    if (replay/'evaluated.json').exists():replay_result=read_json(replay/'evaluated.json')
    else:
        trace=await runtime.execute_case(run,task,recipes[0],initial,replay)
        a=evaluate(spec['requirements'],replay/'initial.db',replay/'final.db',trace)
        b=evaluate(independent.get('requirements',[]),replay/'initial.db',replay/'final.db',trace)
        toolchecks=[{'step':x['step'],**query_check(x['sql'],replay/'initial.db',replay/'final.db',trace)} for x in spec.get('tool_checks',[])]
        replay_result={'failure':trace['failure'],'checks_a':a,'checks_b':b,'tool_checks':toolchecks,'passed':not trace['failure'] and bool(a) and bool(b) and all(x.get('valid') and x['passed'] for x in [*a.values(),*b.values(),*toolchecks]),'trajectory':trace['trajectory'],'db_changes':differences(full_state(initial),full_state(replay/'final.db'))}
        save_json(replay/'evaluated.json',replay_result)
    return cases,replay_result


def extract_official(entry,folder):
    folder=Path(folder);out=folder/'sql'
    out.mkdir(exist_ok=True)
    for n in ('initial','final'):backup(folder/(n+'.db'),out/(n+'.db'))
    hashes={n:digest(out/(n+'.db')) for n in ('initial','final')}
    request={'entry':entry,'initial':str(out/'initial.db'),'final':str(out/'final.db')};save_json(out/'request.json',request)
    try:
        proc=subprocess.run([sys.executable,str(Path(__file__).resolve()),'extract','--request',str(out/'request.json')],capture_output=True,text=True,timeout=40)
        (out/'stdout.log').write_text(proc.stdout);(out/'stderr.log').write_text(proc.stderr)
        data=read_json(out/'result.json') if (out/'result.json').exists() else {'verdict':'judge_error','error':'worker_failed'}
        data['databases_unchanged']=all(digest(out/(n+'.db'))==hashes[n] for n in hashes)
        data['valid']=data.get('verdict')!='judge_error' and data['databases_unchanged'] and proc.returncode==0
    except Exception as e:data={'valid':False,'error':str(e)}
    save_json(out/'checked.json',data);return data


async def task_run(run,ledger_id):
    run=Path(run).resolve();m=read_json(run/'manifest.json');task=next(t for t in m['tasks'] if t['ledger_id']==ledger_id)
    folder=run/'tasks'/ledger_id;folder.mkdir(parents=True,exist_ok=True)
    if (folder/'result.json').exists():return read_json(folder/'result.json')
    save_json(folder/'worker_source.json',{'at':now(),'files':{p.name:digest(p) for p in Path(__file__).parent.glob('*.py')}})
    result={'ledger_id':ledger_id,'scenario':task['scenario'],'task_idx':task['task_idx'],'task':task['task'],'state':'pending_build','layer1':'pending','layer2':'pending','reasons':[],'started_at':now(),'case_count':0,'judged_cases':[]}
    try:
        env=read_json(run/'environments'/task['scenario']/'result.json')
        if env['hard_defect']:
            result.update(state='reject',layer1='reject',layer2='not_run',reasons=['reproduced_initialization_defect']);return result
        if not env.get('runtime_discovery_passed'):
            result.update(state='blocked_run',reasons=['runtime_discovery_failed']);return result
        confirmed_path=run/'confirmed_defects.jsonl'
        if confirmed_path.exists():
            scoped=next((f for f in clean_data.rows(confirmed_path) if f['ledger_id']==ledger_id),None)
            if scoped:
                result.update(scoped);return result
        assets=configure_runtime(run)
        entries=[r for r in assets['gen_verifier.jsonl'] if r['scenario']==task['scenario'] and r['task_idx']==task['task_idx']]
        binding=clean_data.verifier_binding(entries,task['task']);result['binding']=binding
        if not binding['usable']:
            result.update(state='review',reasons=['verifier_binding:'+binding['reason']]);return result
        model=Model(run)
        contract=compact_contract(next(r['api_spec'] for r in assets['gen_spec.jsonl'] if r['scenario']==task['scenario']))
        schema=next(r['db_schema'] for r in assets['gen_db.jsonl'] if r['scenario']==task['scenario'])
        payload={'task':task['task'],'public_contract':contract,'schema':schema,'clock':m['clock']}
        # Neither extraction sees verifier code, historical oracle, Judge outputs or the other extraction.
        spec,independent=await asyncio.gather(model.json(folder/'build_0','build',prompts.BUILD,payload),model.json(folder/'independent_0','extract',prompts.EXTRACT,payload))
        initial=run/'environments'/task['scenario']/'initial.db'
        review=None;cases=[];replay={};issues=[]
        # All remaining tasks receive initial coverage before repair budget is expanded.
        max_rounds=1 if (run/'breadth_first.flag').exists() else 3
        for round_idx in range(max_rounds):
            roundfolder=folder/f'round_{round_idx}';roundfolder.mkdir(exist_ok=True)
            save_json(roundfolder/'requirements.json',spec);save_json(roundfolder/'independent.json',independent)
            issues=validate_config(spec,task['task'],json.dumps(contract,ensure_ascii=False))
            result['requirements_count']=len(spec.get('requirements',[]));result['independent_requirements_count']=len(independent.get('requirements',[]))
            # Candidate ambiguity notes require evidence review; resolved contract facts are not blockers.
            try:cases,replay=await execute_cases(run,task,spec,independent,roundfolder)
            except Exception as e:
                cases=[];replay={};issues.append('candidate_execution:'+str(e))
            matrix=coverage(spec,cases);save_json(roundfolder/'coverage.json',matrix)
            issues+=['coverage_missing:'+x['requirement_id'] for x in matrix if x['status']=='missing']
            if not replay.get('passed'):issues.append('positive_replay_incomplete')
            for c in cases:
                if not c['label_valid']:issues.append('unverified_label:'+c['name'])
                if c['expected'] and not all(x['passed'] for x in c['tool_checks']):issues.append('tool_business_check:'+c['name'])
            evidence={'task':task['task'],'public_contract':contract,'schema':schema,'initial_state':relevant_state(initial,spec,independent,cases),'first_extraction':spec,'independent_extraction':independent,'cases':[review_case(c) for c in cases],'replay':review_case(replay),'coverage':matrix,'mechanical_issues':issues}
            # Reviews can verify sound negative cases even when other coverage remains unfinished.
            review=await model.json(roundfolder/'review','review',prompts.REVIEW+' Also return "verified_case_ids": a list of cases whose labels you can prove from actual facts. A negative needs at least one clear violation; a positive needs ALL requirements satisfied. Missing other cases prevents keep but need not invalidate an independently proven negative.',evidence,max_tokens=8000)
            result['last_round']=round_idx;result['review']=review;result['case_count']=len(cases);result['coverage']=matrix
            if not issues and review.get('approved') and all(review.get(k) for k in ('label_verified','layer1_verified','provenance_verified','coverage_verified','input_verified')):break
            if review.get('ambiguities') and round_idx==max_rounds-1:
                result.update(state='review',reasons=['unresolved_semantic_ambiguity'],ambiguities=review['ambiguities']);break
            if round_idx<max_rounds-1:
                repair={**payload,'previous_config':spec,'independent_extraction':independent,'actual_evidence':{'cases':[review_case(c) for c in cases],'replay':review_case(replay)},'issues':issues,'review':review}
                spec=await model.json(folder/f'build_{round_idx+1}','build',prompts.REPAIR,repair)
                # Fix SQL syntax/reference errors in independent extraction without exposing first extraction or Judge.
                berr=[{'case':c['name'],'checks':c['checks_b'],'trajectory':c['trajectory'],'db_changes':c['db_changes']} for c in cases if any(not x.get('valid') for x in c['checks_b'].values())]
                if berr or review.get('issues') or review.get('ambiguities'):
                    independent=await model.json(folder/f'independent_{round_idx+1}','extract',prompts.EXTRACT+' Repair the prior SQL execution errors using supplied actual evidence, retaining all task conditions.',{**payload,'prior':independent,'sql_errors':berr,'review_findings':review.get('issues',[]),'ambiguity_review':review.get('ambiguities',[])})
        if result['state']=='review':return result
        result['reasons']=sorted(set(issues+([] if review and review.get('approved') else ['semantic_review_not_approved'])))
        all_gates=not result['reasons'] and review and all(review.get(k) for k in ('label_verified','layer1_verified','provenance_verified','coverage_verified','input_verified'))
        if all_gates:result['layer1']='passed_task_dependencies'
        elif replay.get('passed') and review and review.get('layer1_verified'):result['layer1']='passed_task_dependencies'
        verified_ids=set((review or {}).get('verified_case_ids',[]))
        verified=[c for c in cases if c['label_valid'] and c['name'] in verified_ids and (all_gates or not c['expected'])]
        # Freeze actual tested cases and independent labels before first official query/Judge.
        freeze={'spec_sha256':digest(spec),'independent_sha256':digest(independent),'review_sha256':digest(review),'cases':[{'name':c['name'],'expected':c['expected'],'path':c['path'],'label_sha256':digest(c),'initial_sha256':digest(Path(c['path'])/'initial.db'),'final_sha256':digest(Path(c['path'])/'final.db'),'trajectory_sha256':digest(Path(c['path'])/'trajectory.json')} for c in verified],'all_keep_gates_before_judge':bool(all_gates),'frozen_at':now()}
        save_json(folder/'frozen_formal_cases.json',freeze)
        result['formal_cases']=len(verified)
        if not verified:return result
        no_error=True
        # Test each formal case; early stop only after evidence review confirms a defect.
        for c in verified:
            extraction=extract_official(entries[0],c['path'])
            case_result={'name':c['name'],'expected':c['expected'],'slots':[],'path':c['path']}
            result['judged_cases'].append(case_result)
            if not extraction['valid']:
                case_result['sql_error']=extraction;result['reasons'].append('official_sql_execution_error');no_error=False;continue
            for slot in range(5):
                jf=Path(c['path'])/'judge'/str(slot)
                verdict=await model.judge(jf,task['task'],extraction['details'],c['trajectory'])
                case_result['slots'].append({'slot':slot,**verdict})
                if not verdict['valid']:
                    no_error=False;result['reasons'].append('invalid_judge_output');continue
                if verdict['accepted']!=c['expected']:
                    no_error=False
                    audit=await model.json(Path(c['path'])/'dispute'/str(slot),'dispute',prompts.DISPUTE,{'task':task['task'],'public_contract':contract,'schema':schema,'initial_state':full_state(initial),'requirements':spec,'independent':independent,'case':c,'actual_official_request':read_json(jf/'official_request.json'),'verdict':verdict},max_tokens=6000)
                    case_result['dispute']=audit
                    if audit.get('confirmed_misclassification') and audit.get('adapter_correct') and audit.get('label_correct') and not audit.get('ambiguities') and not audit.get('label_errors'):
                        result.update(state='reject',layer2='judge_defect',reasons=['confirmed_judge_misclassification'],early_stop='not_run_due_to_confirmed_defect');return result
                    result.update(state='review',layer2='disputed',reasons=['judge_label_disagreement_not_confirmed']);return result
            save_json(folder/'progress.json',result)
        if all_gates and no_error and len(verified)==len(cases):result.update(state='keep',layer2='passed_finite_suite',reasons=[])
        elif all_gates and not no_error:result.update(state='blocked_run',layer2='invalid_or_incomplete_judge')
    except Exception as e:
        result['exception']={'type':type(e).__name__,'error':str(e),'traceback':traceback.format_exc()}
        result['reasons'].append('runtime_or_model_failure:'+type(e).__name__)
        if any(s in str(e).lower() for s in ('budget','timeout','connection','invalid_model_finish','response','api')) or type(e).__name__.startswith(('API','RateLimit','Authentication')):result['state']='blocked_run'
    finally:
        result['finished_at']=now();save_json(folder/'result.json',result)
        print(json.dumps({k:result.get(k) for k in ('ledger_id','scenario','task_idx','state','layer1','case_count','formal_cases','reasons')},ensure_ascii=False),flush=True)
    return result


def report(run):
    run=Path(run);m=read_json(run/'manifest.json');records=[]
    for t in m['tasks']:
        p=run/'tasks'/t['ledger_id']/'result.json'
        records.append(read_json(p) if p.exists() else {'ledger_id':t['ledger_id'],'scenario':t['scenario'],'task_idx':t['task_idx'],'task':t['task'],'state':'pending_build','reasons':['not_started']})
    counts=dict(collections.Counter(r['state'] for r in records));old=clean_data.rows(ROOT/'evidence/legacy/cleaning/task_certificates.jsonl');oldmap={r['task']['ledger_id'] if isinstance(r.get('task'),dict) else r.get('ledger_id'):r for r in old}
    oldcounts=dict(collections.Counter(r.get('decision') for r in old if (r['task']['ledger_id'] if isinstance(r.get('task'),dict) else r.get('ledger_id')) in m['pilot100']))
    calls=[]
    with sqlite3.connect(run/'budget.sqlite') as db:
        db.row_factory=sqlite3.Row;calls=[dict(x) for x in db.execute('SELECT * FROM requests ORDER BY id')]
    callkeys={c['key'] for c in calls}
    for r in records:
        if 'invalid_judge_output' not in r.get('reasons',[]):continue
        original=list(r['reasons']);reasons=[x for x in original if x!='invalid_judge_output']
        for c in r.get('judged_cases',[]):
            for s in c['slots']:
                if s['valid']:continue
                p=Path(c['path'])/'judge'/str(s['slot'])
                if (p/'response.json').exists():reasons.append('invalid_judge_response')
                elif str(p.resolve()) in callkeys or str((p/'transport_resume_1').resolve()) in callkeys:reasons.append('judge_transport_failure')
                else:reasons.append('judge_not_dispatched_budget_limit')
        r['raw_worker_reasons']=original;r['reasons']=list(dict.fromkeys(reasons))
    tokens=collections.Counter();stages=collections.Counter();request_status=collections.Counter()
    for c in calls:
        stages[c['stage']]+=1;request_status[c['status']]+=1
        if c['usage']:
            u=json.loads(c['usage'])
            for k in ('prompt_tokens','completion_tokens','total_tokens'):tokens[k]+=u.get(k,0)
    metrics=collections.Counter();badcases=set();badtasks=set();formal=collections.Counter();callkeys={c['key'] for c in calls}
    for r in records:
        freeze=Path(r.get('evidence_dir',run/'tasks'/r['ledger_id']))/'frozen_formal_cases.json'
        if freeze.exists():
            for c in read_json(freeze)['cases']:formal['positive' if c['expected'] else 'negative']+=1
        for c in r.get('judged_cases',[]):
            for s in c['slots']:
                metrics['slots']+=1
                slotpath=Path(c['path'])/'judge'/str(s['slot'])
                dispatched=str(slotpath.resolve()) in callkeys or str((slotpath/'transport_resume_1').resolve()) in callkeys
                if not dispatched:metrics['not_dispatched']+=1;continue
                metrics['dispatched_slots']+=1
                if not (slotpath/'response.json').exists():metrics['transport_or_usage_unknown']+=1;continue
                if not s['valid']:metrics['invalid_response']+=1;continue
                kind='positive' if c['expected'] else 'negative';metrics['valid_'+kind]+=1
                if s['accepted']!=c['expected']:
                    metrics['raw_disagreement_'+kind]+=1;badcases.add((r['ledger_id'],c['name']));badtasks.add(r['ledger_id'])
                    dispute=c.get('root_dispute',c.get('dispute',{}))
                    if r['state']=='reject' and all(dispute.get(k) for k in ('confirmed_misclassification','label_correct','adapter_correct')) and not dispute.get('ambiguities') and not dispute.get('label_errors'):metrics['false_reject' if c['expected'] else 'false_accept']+=1
    cost={'physical_requests':len(calls),'preflight_physical_requests':1,'preflight_tokens':(m.get('preflight') or {}).get('usage',{}),'tokens':dict(tokens),'by_stage':dict(stages),'status':dict(request_status),'currency_cost':None,'price_verified':False,'caps':m['budget'],'transport_retries':0}
    summary={'task_denominator':100,'same_ids_and_order':m['pilot100']==read_json(ROOT/'evidence/legacy/cleaning/manifest.json')['pilot100'],'old_counts':oldcounts,'new_counts':counts,'layer1_dependency_passed':sum(r.get('layer1')=='passed_task_dependencies' for r in records),'requirements_extracted':sum(bool(r.get('requirements_count')) for r in records),'dual_requirements_extracted':sum(bool(r.get('independent_requirements_count')) for r in records),'formal_cases':dict(formal),'judge_metrics':dict(metrics),'cases_with_disagreement':len(badcases),'tasks_with_disagreement':len(badtasks),'pending_reasons':dict(collections.Counter(x for r in records if r['state'] not in ('keep','reject') for x in r.get('reasons',[]))),'complete':not any(r['state'] in ('pending_build','blocked_run','review') for r in records)}
    save_json(run/'summary.json',summary);save_json(run/'cost_report.json',cost);write_rows(run/'tasks.ledger.jsonl',records)
    for state in ('keep','reject','pending_build','blocked_run','review'):write_rows(run/f'tasks.{state}.jsonl',[r for r in records if r['state']==state])
    write_rows(run/'build_queue.jsonl',[{'ledger_id':r['ledger_id'],'reasons':r.get('reasons',[]),'next_action':'Repair listed construction/coverage failures; invalidate dependent labels and Judge evidence before new version'} for r in records if r['state']=='pending_build'])
    write_rows(run/'review_queue.jsonl',[r for r in records if r['state']=='review'])
    for out,field in [('requirements.jsonl','requirements_count'),('coverage.jsonl','coverage')]:write_rows(run/out,[{'ledger_id':r['ledger_id'],field:r.get(field),'evidence_dir':str(run/'tasks'/r['ledger_id'])} for r in records])
    write_rows(run/'task_dependencies.jsonl',[{'ledger_id':r['ledger_id'],'status':r.get('layer1'),'evidence':str(run/'tasks'/r['ledger_id'])} for r in records])
    lines=['# 原100题：两层清洗与旧方案实测对比','',f"生成时间：{now()}。固定同一批 **100题**、原顺序、数据版本 `{m['revision']}`。",'', '| 任务级指标 | 旧认证流程 | 本轮两层流程 |','|---|---:|---:|',f"| keep | {oldcounts.get('keep',0)} | {counts.get('keep',0)} |",f"| reject | {oldcounts.get('reject',0)} | {counts.get('reject',0)} |",f"| 尚未通过 | {oldcounts.get('quarantine',0)} | {sum(counts.get(k,0) for k in ('pending_build','blocked_run','review'))} |",'',f"新状态明细：`{counts}`。第一层任务依赖范围通过 {summary['layer1_dependency_passed']}/100；完成两份独立条件提取 {summary['dual_requirements_extracted']}/100。",'', '## 案例与判定', '', f"冻结且复核的正式案例：`{dict(formal)}`。Judge slot 统计：`{dict(metrics)}`。至少一次原始分歧：{len(badcases)} 个案例、{len(badtasks)} 道题。",'', '每个案例的5次slot是重复判定，不能视作5个独立案例。误接受以有效负例slot为分母，误拒绝以有效正例slot为分母；无效输出另列。新旧案例和标签体系不同，不直接将历史216/224与本轮率作同口径准确率提升。', '', '## 用量', '',f"本轮正式流程物理请求 {len(calls)} 次；token `{dict(tokens)}`。另有1次连通性检查，70 tokens。各阶段请求：`{dict(stages)}`。无可靠单价，金额留空。",'', '## 逐题结果', '', '| 题目 | 旧状态 | 新状态 | 第一层 | 原因 |','|---|---|---|---|---|']
    for r in records:
        prior=oldmap.get(r['ledger_id'],{}).get('decision','unknown');reason='; '.join(r.get('reasons',[])).replace('|','/')
        lines.append(f"| {r['scenario']}:{r['task_idx']} | {prior} | {r['state']} | {r.get('layer1','pending')} | {reason} |")
    lines+=['','## 证据边界','','keep仅表示有限冻结测试通过；未完成项保留在工程、语义或运行队列。模型独立复提取与复核是辅助检查，正确标签仍以题面、真实工具记录及数据库独立查询为依据。未测工具不代表整个环境全部通过。原始数据、旧结果和官方Judge均未修改。']
    (run/'report_zh.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


async def batch(run,ids,jobs):
    sem=asyncio.Semaphore(jobs)
    async def one(i):
        async with sem:
            folder=Path(run)/'tasks'/i;folder.mkdir(parents=True,exist_ok=True)
            with (folder/'worker.log').open('a') as f:
                p=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).resolve()),'task','--run',str(run),'--task',i,stdout=f,stderr=asyncio.subprocess.STDOUT,env={**os.environ,'TZ':'Asia/Shanghai','PYTHONUNBUFFERED':'1'})
                rc=await p.wait()
            result=read_json(folder/'result.json') if (folder/'result.json').exists() else {'state':'worker_crash'}
            print(json.dumps({'task':i,'exit_code':rc,'state':result['state'],'reasons':result.get('reasons',[])},ensure_ascii=False),flush=True)
    await asyncio.gather(*(one(i) for i in ids))
    report(run)


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','environments','task','batch','report','extract']);p.add_argument('--run',type=Path,default=ROOT/'cleaning_runs/two_layer_pilot100_v1');p.add_argument('--task');p.add_argument('--tasks',nargs='*');p.add_argument('--jobs',type=int,default=3);p.add_argument('--request',type=Path);args=p.parse_args()
    if args.command=='prepare':prepare(args.run)
    elif args.command=='environments':asyncio.run(environments(args.run))
    elif args.command=='task':asyncio.run(task_run(args.run,args.task))
    elif args.command=='batch':
        m=read_json(args.run/'manifest.json');asyncio.run(batch(args.run,args.tasks or m['pilot100'],args.jobs))
    elif args.command=='report':report(args.run)
    elif args.command=='extract':
        from awm.core.verify import run_verifier
        request=read_json(args.request);verdict,details=run_verifier(request['entry'],'sql',request['initial'],request['final'])
        save_json(args.request.parent/'result.json',{'verdict':verdict,'details':details})

if __name__=='__main__':main()
