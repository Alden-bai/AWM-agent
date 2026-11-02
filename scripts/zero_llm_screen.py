"""Extend structural screening with isolated offline workers and explicit coverage."""
import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from clean_data import ROOT, canonical, normalize, sha, write_json, write_rows
from zero_llm_checks import cached_recipe, curated_recipe
from zero_llm_guard import clean_env


def worker(job):
    folder = Path(job['folder'])
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder/'job.json', job)
    started = time.monotonic()
    with (folder/'worker.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/zero_llm_worker.py'), str(folder/'job.json')],
                                env=clean_env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            code = 'timeout'
    outcome = {'exit_code': code, 'seconds': round(time.monotonic()-started, 3), 'log': str(folder/'worker.log')}
    if code == 0 and (folder/'result.json').exists():
        outcome['result'] = json.loads((folder/'result.json').read_text())
    write_json(folder/'process.json', outcome)
    return outcome


def classify(row, runtime):
    if row['decision'] == 'reject':
        return 'excluded', ['environment_initialization_defect']
    if not row['verifier_binding']['usable']:
        return 'excluded', ['judge_binding_unusable:' + row['verifier_binding']['reason']]
    if not runtime or runtime.get('exit_code') != 0:
        return 'not_covered', ['runtime_worker_failed_or_timeout']
    result = runtime['result']
    if result['tool_schema_errors']:
        return 'not_covered', ['published_tool_schema_invalid']
    sql = result['sql_checks']
    if not sql:
        return 'not_covered', ['judge_program_not_executed']
    if any(x.get('repeat', {}).get('same_failure') for x in sql if x.get('repeat')):
        return 'excluded', ['reproduced_judge_program_error']
    if any(not x['db_unchanged'] for x in sql):
        return 'excluded', ['judge_program_modified_database']
    if any(not x['native_payload_exact'] for x in sql):
        return 'not_covered', ['judge_capture_adapter_mismatch']
    probes = result['probes']
    if any(p['failure'] for p in probes):
        return 'not_covered', ['runtime_probe_failed_requires_attribution']
    if not probes or not probes[0]['actual_calls']:
        return 'not_covered', ['task_tool_probe_missing']
    if any(p['business']['status'] == 'mismatch' for p in probes):
        return 'not_covered', ['business_mismatch_requires_dependency_review']
    if any(s.get('status') == 'diagnostic_mismatch' for p in probes for s in p['step_checks']):
        return 'not_covered', ['tool_response_database_mismatch_requires_review']
    return 'screen_pass', ['finite_program_checks_passed_semantics_unchecked']


def source_review(cache, task, environment):
    # Previously reviewed absence of alternative APIs is used only if its source hash still matches.
    import hashlib
    folder = cache/'confirmed_environment_defects'/task['ledger_id']
    files = list(folder.glob('**/source_review.json'))
    expected = hashlib.sha256(json.dumps(environment['full_code'], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    raw = hashlib.sha256(environment['full_code'].encode()).hexdigest()
    for p in files:
        record = json.loads(p.read_text())
        if record.get('source_sha256') in (expected, raw):
            return {'path': str(p), 'sha256': sha(p), 'environment_code_hash_matched': True}
    return None


def extend(out, baseline, assets, cache, reuse_results=False):
    baseline_dir = out/'structural'
    if not reuse_results:
        baseline_dir.mkdir()
    for name in ('summary.json', 'tasks.ledger.jsonl', 'tasks.retain.jsonl', 'tasks.reject.jsonl', 'report_zh.md'):
        if not reuse_results:
            (baseline_dir/name).write_bytes((out/name).read_bytes())
        else:
            archive = out/'before_serialization_audit'
            archive.mkdir(exist_ok=True)
            (archive/name).write_bytes((out/name).read_bytes())
    started = time.monotonic()
    lookup = {}
    for name, records in assets.items():
        grouped = {}
        for record in records:
            key = normalize(record.get('scenario', record.get('name', '')))
            grouped.setdefault(key, []).append(record)
        lookup[name] = grouped
    jobs = []
    for task in baseline:
        if task['decision'] == 'reject' or task['asset_issues']:
            continue
        name = normalize(task['scenario'])
        environment = lookup['gen_envs.jsonl'][name][0]
        contract = json.dumps(lookup['gen_spec.jsonl'][name][0]['api_spec'], ensure_ascii=False)
        recipe = curated_recipe(task)
        if recipe:
            recipe['reviewed_business'] = True
        else:
            recipe = cached_recipe(cache, task, contract)
        v = [assets['gen_verifier.jsonl'][i] for i in task['asset_indices']['gen_verifier.jsonl']]
        job = {'folder': str(out/'runtime'/task['ledger_id']), 'task': {k: task[k] for k in ('ledger_id', 'scenario', 'task_idx', 'task')},
               'initial': str(out/'environments'/name/'initial.db'), 'environment': environment,
               'verifier': v[0] if task['verifier_binding']['usable'] else None, 'recipe': recipe}
        if reuse_results:
            job = json.loads((Path(job['folder'])/'job.json').read_text())
        jobs.append(job)
    write_json(out/('offline_finalization.json' if reuse_results else 'runtime_manifest.json'), {'protocol': 'zero_llm_program_screen_v1', 'selected_ids': [r['ledger_id'] for r in baseline],
        'fresh_model_requests': 0, 'model_credentials_loaded': False, 'network': 'Python socket guard: loopback only, in worker and MCP child',
        'clean_data_sha256': sha(ROOT/'scripts/clean_data.py'),
        'judge_mode': 'official SQL extraction and captured prompt; NO inference or fabricated model response',
        'runtime_python': sys.executable, 'jobs': len(jobs), 'source_hashes': {p.name: sha(p) for p in (ROOT/'scripts').glob('zero_llm_*.py')},
        'official_sources': {p.name: sha(p) for p in [ROOT/'agent-world-model/awm/core/verify.py', ROOT/'agent-world-model/awm/core/server.py', ROOT/'evidence/legacy/source/awm_audit/runtime.py']},
        'probe_cache': str(cache), 'clock_policy': 'actual wall clock; no relative-date semantics certification'})
    outcomes = {}
    if reuse_results:
        for job in jobs:
            folder = Path(job['folder'])
            result = json.loads((folder/'process.json').read_text())
            if result.get('exit_code') == 0:
                observed = result['result']
                trace = json.loads((folder/'replay_0/trajectory.json').read_text())
                for check in observed['sql_checks']:
                    request = json.loads((folder/'judge_program'/check['variant']/'captured_request.json').read_text())
                    task_json, verification_json = request['messages'][1]['content'].split('\n\nverification_json:\n', 1)
                    payload = json.loads(task_json.removeprefix('task_json:\n'))
                    evidence = json.loads(verification_json)['code_execution_result']
                    native = trace['trajectory'] if check['variant'] == 'after_tools' else []
                    check['native_payload_exact_before_serialization_audit'] = check['native_payload_exact']
                    check['native_payload_exact'] = payload['user_task'] == job['task']['task'] and payload['trajectory'] == native and evidence == check['result']
                observed['offline_serialization_audit'] = 'Compare the actual JSON wire representation with recorded SQL result; tuples and integer keys undergo standard JSON conversion. No tool or model calls repeated.'
                write_json(folder/'audited_result.json', observed)
            outcomes[job['task']['ledger_id']] = result
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for job, result in zip(jobs, pool.map(worker, jobs)):
                task = job['task']
                outcomes[task['ledger_id']] = result
                r = result.get('result', {})
                print(json.dumps({'task': task['ledger_id'], 'worker': result['exit_code'],
                                  'calls': sum(p['actual_calls'] for p in r.get('probes', [])), 'judge_variants': len(r.get('sql_checks', []))}), flush=True)
    results = []
    jobmap = {j['task']['ledger_id']: j for j in jobs}
    for row in baseline:
        run = outcomes.get(row['ledger_id'])
        status, reasons = classify(row, run)
        r = (run or {}).get('result', {})
        probes = r.get('probes', [])
        inherited = None
        if len(probes) == 2 and all(p['business']['status'] == 'mismatch' and not p['failure'] for p in probes):
            job = jobmap[row['ledger_id']]
            inherited = source_review(cache, row, job['environment'])
            if inherited:
                status, reasons = 'excluded', ['reproduced_tool_business_defect_with_matching_dependency_review']
        # Historical semantic Judge failure stays excluded without spending on another judgment.
        previous = cache/'tasks'/row['ledger_id']/'result.json'
        historical = None
        if previous.exists():
            old = json.loads(previous.read_text())
            if old.get('state') == 'reject' and 'confirmed_explicit_answer_false_accept' in old.get('reasons', []):
                oldmanifest = json.loads((cache/'manifest.json').read_text())
                matches = oldmanifest['source']['official_verify_sha256'] == sha(ROOT/'agent-world-model/awm/core/verify.py')
                current_manifest = json.loads((out/'manifest.json').read_text())
                matches = matches and all(oldmanifest['files'][name]['sha256'] == info['sha256'] for name, info in current_manifest['sources'].items())
                if matches and row['task'] == old['task']:
                    historical = {'path': str(previous), 'sha256': sha(previous), 'reason': 'confirmed_explicit_answer_false_accept',
                                  'scope': 'historical SQL+LLM configuration; no new semantic test', 'judge_config': oldmanifest['judge']}
                    status, reasons = 'excluded', ['historical_confirmed_judge_semantic_defect']
        job = jobmap.get(row['ledger_id'])
        results.append({**row, 'screen_status': status, 'screen_reasons': reasons,
                        'decision': 'reject' if status == 'excluded' else 'retain',
                        'training_ready': False, 'semantic_status': 'not_certified',
                        'reward_status': 'blocked_zero_llm_screen' if status == 'excluded' else 'semantic_unvalidated',
                        'reject_reason': ';'.join(reasons) if status == 'excluded' else None,
                        'runtime_evidence': str(out/'runtime'/row['ledger_id']/('audited_result.json' if reuse_results else 'result.json')) if r else None,
                        'runtime_exit_code': (run or {}).get('exit_code'),
                        'actual_tool_calls': sum(p['actual_calls'] for p in probes), 'tool_schema_checked': bool(r.get('tools_discovered')),
                        'task_probe_prefix_only': job['recipe'].get('prefix_only', True) if job else None,
                        'independent_business_check': probes[0]['business']['status'] if probes else 'not_run',
                        'judge_program_variants': len(r.get('sql_checks', [])),
                        'dependency_review': inherited, 'historical_judge_defect': historical})
    write_rows(out/'tasks.ledger.jsonl', results)
    write_rows(out/'review_queue.jsonl', [r for r in results if r['screen_status'] == 'not_covered'])
    for label in ('excluded', 'screen_pass', 'not_covered'):
        write_rows(out/f'tasks.{label}.jsonl', [r for r in results if r['screen_status'] == label])
    for label in ('retain', 'reject'):
        write_rows(out/f'tasks.{label}.jsonl', [r for r in results if r['decision'] == label])
    summary = {'task_denominator': len(results), 'same_original_ids_and_order': [r['ledger_id'] for r in results] == json.loads((ROOT/'evidence/legacy/cleaning/manifest.json').read_text())['pilot100'],
               'screen_statuses': dict(Counter(r['screen_status'] for r in results)),
               'exclusion_reasons': dict(Counter(reason for r in results if r['screen_status'] == 'excluded' for reason in r['screen_reasons'])),
               'tasks_with_actual_tool_calls': sum(r['actual_tool_calls'] > 0 for r in results), 'actual_tool_calls': sum(r['actual_tool_calls'] for r in results),
               'tasks_with_judge_program_execution': sum(r['judge_program_variants'] > 0 for r in results),
               'response_database_field_comparisons': sum(len(s.get('field_checks', [])) for run in outcomes.values() for p in run.get('result', {}).get('probes', []) for s in p['step_checks']),
               'tasks_with_response_database_checks': sum(any(s.get('field_checks') for p in run.get('result', {}).get('probes', []) for s in p['step_checks']) for run in outcomes.values()),
               'business_check_statuses': dict(Counter(r['independent_business_check'] for r in results)),
               'fresh_model_requests': 0, 'fresh_model_tokens': 0, 'new_api_cost': 0,
               'semantic_judge_tested_this_run': False, 'seconds': json.loads((out/'before_serialization_audit/summary.json').read_text())['seconds'] if reuse_results else round(time.monotonic()-started, 2),
               'offline_finalization_seconds': round(time.monotonic()-started, 2) if reuse_results else None,
               'worker_failures': {r['ledger_id']: r['runtime_exit_code'] for r in results if r['runtime_exit_code'] not in (None, 0)},
               'external_network_attempts_blocked': sum(len(p.read_text().splitlines()) for p in out.glob('runtime/**/network_denials.jsonl'))}
    write_json(out/'summary.json', summary)
    lines = ['# 原100题零 LLM 程序筛查', '', '本轮只测试环境、工具运行、数据库及官方 SQL Judge 程序，不执行模型判分。', '',
             f"同一批 {len(results)} 题；ID 与顺序一致：{summary['same_original_ids_and_order']}。", '',
             '| 本轮状态 | 题数 | 含义 |', '|---|---:|---|']
    meanings = {'excluded': '当前环境或判分配置不可用；含明确标注的历史语义缺陷', 'screen_pass': '已执行的有限程序探针通过；不代表完整任务或 Judge 语义通过', 'not_covered': '探针缺失或失败待归因；不作为坏题，暂不进入程序通过集'}
    lines += [f"| {k} | {summary['screen_statuses'].get(k, 0)} | {v} |" for k, v in meanings.items()]
    lines += ['', f"真实调用工具的任务：{summary['tasks_with_actual_tool_calls']}/{len(results)}；调用次数（含复现）：{summary['actual_tool_calls']}。",
              f"实际执行官方 SQL Judge 程序：{summary['tasks_with_judge_program_execution']}/{len(results)} 题。",
              '新模型请求 0，tokens 0，新增模型 API 费用 0。本地计算与既有证据制作成本不包含在内。', '',
              '## 覆盖边界', '',
              '- 旧配方只复用调用参数，不复用模型标签；字面值须来自题面或可验证的公开契约引用，动态 ID 只允许来自前序真实工具返回。',
              '- 每步校验真实 MCP inputSchema，记录响应、数据库差异及可映射响应字段与数据库的对照。错误参数或探针失败不自动排除题目。',
              '- 共享数据库对照只检查已执行端点的可比字段；独立业务模板及每题覆盖范围见台账。其余聚合、筛选和业务语义没有完整认证。',
              '- 官方 SQL 在初态和可获得的真实工具末态上执行；失败另用全新副本复现。unchanged 仅为运行探针，不标记为错误答案。',
              '- 原版 Judge 提示词在发送边界被本地捕获；没有发送请求，没有伪造模型输出，未测试模型判断或响应解析。',
              '- 工具缺陷只在双实例复现并且历史依赖审查的源码哈希匹配时升级排除；历史 Judge 语义缺陷单列，不冒充本次发现。',
              '- 环境启动与某题共享，不代表该题所有依赖工具经过测试；screen_pass 不能直接等同 training_ready。', '',
              '## 排除原因', '', '| 原因 | 题数（可重叠） |', '|---|---:|']
    lines += [f'| {k} | {v} |' for k, v in summary['exclusion_reasons'].items()]
    lines += ['', '## 逐题结果', '', '| 题目 | 状态 | 工具调用数 | SQL探针数 | 原因 |', '|---|---|---:|---:|---|']
    lines += [f"| {r['task_id']} | {r['screen_status']} | {r['actual_tool_calls']} | {r['judge_program_variants']} | {'; '.join(r['screen_reasons'])} |" for r in results]
    lines += ['', '结构筛查原始输出保留在 structural/；每题 runtime/ 下保存任务、配方来源、真实 MCP 响应、初末数据库、逐步核对、SQL输出和截获的Judge输入。', '']
    (out/'report_zh.md').write_text('\n'.join(lines))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    import argparse
    from clean_data import FILES, rows
    parser = argparse.ArgumentParser(description='Reconcile recorded Judge JSON payloads offline; no execution or model requests.')
    parser.add_argument('--reconcile', type=Path, required=True)
    args = parser.parse_args()
    out = args.reconcile.resolve()
    manifest = json.loads((out/'manifest.json').read_text())
    assets = {name: rows(Path(manifest['sources'][name]['path'])) for name in FILES}
    for name in FILES:
        if sha(Path(manifest['sources'][name]['path'])) != manifest['sources'][name]['sha256']:
            raise ValueError('Source hash mismatch')
    cache = Path(json.loads((out/'runtime_manifest.json').read_text())['probe_cache'])
    extend(out, rows(out/'structural/tasks.ledger.jsonl'), assets, cache, reuse_results=True)
