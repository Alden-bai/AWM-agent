"""Isolated worker: real local MCP calls and official SQL extraction, never LLM inference."""
import ast
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evidence/legacy/source'))
sys.path.insert(0, str(ROOT / 'agent-world-model'))
from zero_llm_guard import install
from zero_llm_checks import state, delta, resolve, business_check, tool_results
from clean_data import write_json, canonical


def endpoint_info(code):
    tree = ast.parse(code)
    models = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__tablename__' for t in stmt.targets) and isinstance(stmt.value, ast.Constant):
                    models[node.name] = stmt.value.value
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def response_paths(annotation, path=(), seen=()):
        if isinstance(annotation, ast.Subscript):
            name = ast.unparse(annotation.value)
            if name in ('List', 'list', 'Sequence'):
                return response_paths(annotation.slice, path+('*',), seen)
            if name == 'Optional':
                return response_paths(annotation.slice, path, seen)
            return []
        if not isinstance(annotation, ast.Name) or annotation.id in seen:
            return []
        name = annotation.id
        base = name
        for suffix in ('Model', 'Response', 'Read', 'Out', 'Schema'):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        if base in models:
            mapping = {'path': list(path), 'table': models[base], 'model': name}
            temporal = [f.target.id for f in (classes[name].body if name in classes else [])
                        if isinstance(f, ast.AnnAssign) and isinstance(f.target, ast.Name)
                        and any(isinstance(n, ast.Name) and n.id in ('datetime', 'date', 'time') for n in ast.walk(f.annotation))]
            if temporal:
                mapping['temporal_fields'] = temporal
            return [mapping]
        if name not in classes:
            return []
        found = []
        for field in classes[name].body:
            if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                # Respect explicit Pydantic serialization aliases.
                key = field.target.id
                if isinstance(field.value, ast.Call):
                    for kw in field.value.keywords:
                        if kw.arg in ('alias', 'serialization_alias') and isinstance(kw.value, ast.Constant):
                            key = kw.value.value
                found.extend(response_paths(field.annotation, path+(key,), seen+(name,)))
        return found

    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ('get', 'post', 'put', 'patch', 'delete'):
                alias = next((k.value.value for k in dec.keywords if k.arg == 'operation_id' and isinstance(k.value, ast.Constant)), node.name)
                annotation = ast.unparse(node.returns) if node.returns else ''
                base = annotation
                for suffix in ('Response', 'Read', 'Out'):
                    if base.endswith(suffix):
                        base = base[:-len(suffix)]
                        break
                out[alias] = {'method': dec.func.attr, 'table': models.get(base), 'function': node.name, 'response_annotation': annotation, 'response_paths': response_paths(node.returns)}
    return out


def consistency(info, args, response, before, after):
    checks = {'db_changes': delta(before, after), 'field_checks': [], 'status': 'not_covered'}
    if info.get('method') == 'get':
        checks['read_state_unchanged'] = not checks['db_changes']
    mappings = info.get('response_paths', []) or ([{'path': [], 'table': info['table']}] if info.get('table') else [])
    checks['row_checks'] = []
    for mapping in mappings:
        objects = [response]
        for key in mapping['path']:
            expanded = []
            for obj in objects:
                if key == '*' and isinstance(obj, list):
                    expanded.extend(obj)
                elif isinstance(obj, dict) and key in obj:
                    expanded.append(obj[key])
            objects = expanded
        table = mapping['table']
        if table not in after:
            continue
        for obj in objects:
            if not isinstance(obj, dict) or 'id' not in obj:
                continue
            rows = [r for r in after[table] if r.get('id') == obj['id']]
            checks['row_checks'].append({'table': table, 'id': obj['id'], 'found': len(rows) == 1, 'response_path': mapping['path']})
            if len(rows) == 1:
                for key, value in obj.items():
                    # Dates, encoded JSON and computed projections require separate templates.
                    if key not in mapping.get('temporal_fields', []) and key in rows[0] and isinstance(value, (int, float, bool, str)) and not (
                            isinstance(value, str) and ('date' in key or 'time' in key or key.endswith('_at'))):
                        checks['field_checks'].append({'table': table, 'id': obj['id'], 'field': key, 'returned': value, 'stored': rows[0][key], 'equal': value == rows[0][key]})
    if checks['row_checks']:
        checks['status'] = 'passed' if all(c['found'] for c in checks['row_checks']) and all(c['equal'] for c in checks['field_checks']) else 'diagnostic_mismatch'
    return checks


async def execute(job):
    from awm_audit import runtime
    from awm_audit.common import backup
    from awm.core import verify
    import jsonschema
    folder = Path(job['folder'])
    folder.mkdir(parents=True, exist_ok=True)
    install(str(folder / 'network_denials.jsonl'))
    env_asset = job['environment']
    runtime.AWM = ROOT / 'agent-world-model'
    runtime.scenario_asset = lambda *_: env_asset
    runtime.resolve = resolve
    original_server = runtime.server_script

    def guarded_server(*args):
        script = Path(original_server(*args))
        wrapper = script.with_name('guarded_server.py')
        wrapper.write_text('import sys, runpy\nsys.path.insert(0, ' + repr(str(ROOT / 'scripts')) + ')\nfrom zero_llm_guard import install\ninstall(' + repr(str(folder / 'network_denials.jsonl')) + ')\nrunpy.run_path(' + repr(str(script)) + ', run_name="__main__")\n')
        return str(wrapper)
    runtime.server_script = guarded_server
    original_session = runtime.session
    infos = endpoint_info(env_asset['full_code'])
    checks = []
    active = {'work': None, 'tools': {}}

    @asynccontextmanager
    async def checked_session(url):
        async with original_session(url) as client:
            class Proxy:
                async def list_tools(self):
                    tools = await client.list_tools()
                    active['tools'] = {t.name: t for t in tools.tools}
                    return tools

                async def call_tool(self, name, arguments):
                    schema = active['tools'][name].inputSchema
                    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(arguments), key=lambda e: str(e.path))
                    if errors:
                        checks.append({'tool': name, 'arguments': arguments, 'input_schema_valid': False, 'errors': [e.message for e in errors]})
                        raise ValueError('Probe arguments violate discovered inputSchema')
                    before = state(active['work'])
                    result = await client.call_tool(name, arguments)
                    after = state(active['work'])
                    plain = runtime.unpack_result(result)
                    candidates = [k for k in infos if name == k or name.startswith(k+'_')]
                    info = infos[max(candidates, key=len)] if candidates else {}
                    checks.append({'tool': name, 'arguments': arguments, 'input_schema_valid': True, 'is_error': result.isError,
                                   'endpoint': info, **consistency(info, arguments, plain, before, after)})
                    write_json(folder / 'step_checks.json', checks)
                    return result
            yield Proxy()
    runtime.session = checked_session
    initial = Path(job['initial'])
    task = job['task']
    recipe = job.get('recipe', {'calls': []})
    probe_results = []
    for repeat in range(2):
        where = folder / f'replay_{repeat}'
        active['work'] = where / 'working.db'
        case = {'name': 'offline_probe', 'calls': recipe['calls'], 'answer': '', 'expected': True}
        offset = len(checks)
        trace = await runtime.execute_case(folder, task, case, initial, where)
        # Record the observed return as final output, not a fabricated success statement.
        outputs = tool_results(trace)
        before, after = state(where / 'initial.db'), state(where / 'final.db')
        business = business_check(task, before, after, trace) if recipe.get('reviewed_business') else {'status': 'not_covered_by_independent_template'}
        evidence = {'path': str(where), 'failure': trace['failure'], 'actual_calls': len(outputs),
                    'db_changes': delta(before, after), 'business': business, 'step_checks': checks[offset:]}
        write_json(where / 'checks.json', evidence)
        probe_results.append(evidence)
        # Discovery/normal diagnostic probes run once; repeat mismatches or failures only.
        suspicious = trace['failure'] or business['status'] == 'mismatch' or any(c.get('status') == 'diagnostic_mismatch' for c in checks[offset:])
        if not suspicious:
            break
    discovery = folder / 'replay_0/tools.json'
    tools = json.loads(discovery.read_text()) if discovery.exists() else []
    schema_errors = []
    for tool in tools:
        try:
            jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])
        except jsonschema.SchemaError as e:
            schema_errors.append({'tool': tool['name'], 'error': str(e)})
    sql_checks = []
    if job.get('verifier'):
        # Initial==final is an execution probe, never a labelled negative example.
        variants = [('unchanged', initial, initial)]
        if probe_results and probe_results[0]['actual_calls']:
            variants.append(('after_tools', folder/'replay_0/initial.db', folder/'replay_0/final.db'))
        for label, first, last in variants:
            sqlfolder = folder / 'judge_program' / label
            sqlfolder.mkdir(parents=True, exist_ok=True)
            backup(first, sqlfolder/'initial.db')
            backup(last, sqlfolder/'final.db')
            pre = [state(sqlfolder/'initial.db'), state(sqlfolder/'final.db')]
            reward, result = verify.run_verifier(job['verifier'], 'sql', str(sqlfolder/'initial.db'), str(sqlfolder/'final.db'))
            repeated = None
            if reward == 'judge_error':
                backup(first, sqlfolder/'repeat_initial.db')
                backup(last, sqlfolder/'repeat_final.db')
                repeat_reward, repeat_result = verify.run_verifier(job['verifier'], 'sql', str(sqlfolder/'repeat_initial.db'), str(sqlfolder/'repeat_final.db'))
                repeated = {'reward': repeat_reward, 'result': repeat_result, 'same_failure': repeat_reward == reward and repeat_result == result}
            post = [state(sqlfolder/'initial.db'), state(sqlfolder/'final.db')]
            write_json(sqlfolder/'sql_result.json', result)
            # Capture original prompt construction at its transport boundary. No response is fabricated.
            class Captured(BaseException):
                pass
            captured = {}
            class Client:
                def __init__(self, **kwargs):
                    self.chat = SimpleNamespace(completions=self)
                async def create(self, **kwargs):
                    captured.update(kwargs)
                    raise Captured()
            verify.AsyncOpenAI = Client
            trace = json.loads((folder/'replay_0/trajectory.json').read_text())
            native = trace['trajectory'] if label == 'after_tools' else []
            try:
                await verify.run_llm_judge(task['task'], result, 'http://127.0.0.1:1/offline', 'EMPTY', 'offline-capture-only', trajectory=native)
            except Captured:
                pass
            write_json(sqlfolder/'captured_request.json', captured)
            prompt = captured.get('messages', [{}, {}])[-1].get('content', '')
            try:
                tasktext, evidence_text = prompt.split('\n\nverification_json:\n', 1)
                task_payload = json.loads(tasktext.removeprefix('task_json:\n'))
                verifier_payload = json.loads(evidence_text)['code_execution_result']
                exact = task_payload['user_task'] == task['task'] and task_payload['trajectory'] == native and verifier_payload == json.loads(json.dumps(verify.sanitize_for_json(result)))
            except (ValueError, KeyError, TypeError):
                exact = False
            sql_checks.append({'variant': label, 'reward': reward, 'repeat': repeated, 'result': result, 'db_unchanged': pre == post, 'native_payload_exact': exact,
                               'captured_before_transport': bool(captured), 'actual_model_requests': 0})
    output = {'task_id': task.get('ledger_id'), 'tools_discovered': len(tools), 'tool_schema_errors': schema_errors,
              'probes': probe_results, 'sql_checks': sql_checks, 'actual_model_requests': 0, 'semantic_judge_tested': False}
    write_json(folder / 'result.json', output)


if __name__ == '__main__':
    job = json.loads(Path(sys.argv[1]).read_text())
    asyncio.run(execute(job))
