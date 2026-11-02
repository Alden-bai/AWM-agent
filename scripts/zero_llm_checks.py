"""Deterministic probe selection and evidence checks; no model dependencies."""
import json
import hashlib
import re
import sqlite3
from collections import Counter
from pathlib import Path


def state(path):
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {t[0]: [dict(r) for r in db.execute('SELECT * FROM "' + t[0].replace('"', '""') + '"')]
                for t in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}


def delta(before, after):
    result = {}
    for table in sorted(set(before) | set(after)):
        a = Counter(json.dumps(r, sort_keys=True) for r in before.get(table, []))
        b = Counter(json.dumps(r, sort_keys=True) for r in after.get(table, []))
        if a != b:
            result[table] = {'removed': [json.loads(r) for r in (a-b).elements()],
                             'added': [json.loads(r) for r in (b-a).elements()]}
    return result


def resolve(value, work=None, results=None):
    results = results or []
    if isinstance(value, list):
        return [resolve(v, work, results) for v in value]
    if not isinstance(value, dict):
        return value
    if '$ref' in value:
        if not isinstance(value['$ref'], int) or not 0 <= value['$ref'] < len(results):
            raise ValueError('Reference must name an earlier public tool result')
        cur = results[value['$ref']]
        for k in value.get('path', []):
            cur = cur[k]
        if 'where' in value:
            wanted = resolve(value['where'], work, results)
            cur = [r for r in cur if all(r.get(k) == v for k, v in wanted.items())]
        if value.get('take') == 'one':
            if len(cur) != 1:
                raise ValueError('Public object lookup is not unique')
            cur = cur[0]
        if 'field' in value:
            cur = cur[value['field']] if isinstance(cur, dict) else [r[value['field']] for r in cur]
        return cur
    if '$calc' in value:
        a, b = [resolve(v, work, results) for v in value['values']]
        return {'add': lambda: a+b, 'subtract': lambda: a-b, 'multiply': lambda: a*b,
                'divide': lambda: a/b}[value['$calc']]()
    if any(k.startswith('$') for k in value):
        raise ValueError('Private DB lookup or unsupported computation is forbidden')
    return {k: resolve(v, work, results) for k, v in value.items()}


def literal_in(value, text):
    if value is None:
        return False
    if isinstance(value, bool):
        return re.search(r'\b' + str(value).lower() + r'\b', text, re.I) is not None
    if isinstance(value, (int, float)):
        # Integer boolean compatibility is handled only in reviewed fixtures.
        return re.search(r'(?<![\w.])' + re.escape(str(value)) + r'(?![\w.])', text.replace(',', '')) is not None
    return isinstance(value, str) and bool(value) and value in text


def safe_value(value, task, step, path, sources, contract):
    if isinstance(value, dict):
        if '$ref' in value:
            if not isinstance(value['$ref'], int) or not 0 <= value['$ref'] < step:
                return False
            # Path/field names describe public output; filter values still need provenance.
            return all(safe_value(v, task, step, path, sources, contract) for v in value.get('where', {}).values())
        if '$calc' in value:
            return value['$calc'] in ('add', 'subtract', 'multiply', 'divide') and len(value.get('values', [])) == 2 and all(
                safe_value(v, task, step, path, sources, contract) for v in value['values'])
        if any(k.startswith('$') for k in value):
            return False
        return all(safe_value(v, task, step, path+(k,), sources, contract) for k, v in value.items())
    if isinstance(value, list):
        return all(safe_value(v, task, step, path+(i,), sources, contract) for i, v in enumerate(value))
    if literal_in(value, task):
        return True
    source = sources.get((step, path), {})
    quote = source.get('quote', '')
    return source.get('kind') == 'public_contract' and bool(quote) and quote in contract and literal_in(value, quote)


def cached_recipe(cache, task, contract):
    folder = cache / 'tasks' / task['ledger_id']
    options = sorted(folder.glob('round_*/requirements.json'), reverse=True)
    candidates = []
    for p in options:
        try:
            spec = json.loads(p.read_text())
            sources = {(s['step'], tuple(s['path'])): s for s in spec.get('argument_sources', [])}
            calls = spec.get('positive', {}).get('calls', [])
            accepted = []
            for i, call in enumerate(calls):
                if not safe_value(call['arguments'], task['task'], i, (), sources, contract):
                    break
                accepted.append(call)
            candidates.append({'calls': accepted, 'source': str(p), 'source_sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'source_calls': len(calls),
                               'prefix_only': len(accepted) != len(calls),
                               'scope': 'runtime diagnostic; historical model labels and SQL checks not trusted'})
        except (KeyError, TypeError, ValueError):
            continue
    return max(candidates, key=lambda x: len(x['calls']), default={'calls': [], 'source': None, 'prefix_only': True})


def curated_recipe(task):
    """Task-literal regression probes; independent checks below never read verifier code."""
    if task['scenario'] != 'education_training_management_1':
        return None
    idx = task['task_idx']
    specs = {
        2: ('create_competency_record', {'apprentice_id': 552, 'body': {'competency_id': 205, 'record_date': '2025-01-10', 'rating': 4, 'comments_short': 'Completed full install with minimal guidance'}}),
        3: ('create_work_log_entry', {'apprentice_id': 552, 'body': {'work_date': '2025-01-12', 'job_type': 'Residential wiring rough-in', 'location': '123 Oak St, Springfield', 'hours_worked': 7.5, 'supervisor_signature_flag': 1}}),
        6: ('create_class_evaluation', {'class_id': 88, 'body': {'apprentice_id': 552, 'score': 86, 'pass_fail': 'pass', 'evaluation_date': '2025-02-06', 'comments': 'Good safety awareness and tool handling'}}),
        7: ('get_apprentice_dashboard_summary', {'apprentice_id': 552, 'start_date': '2025-01-01', 'end_date': '2025-03-31', 'completed_rating_threshold': 3}),
        8: ('update_competency', {'competency_id': 340, 'body': {'level': 'intermediate', 'category': 'pipefitting', 'short_description': 'Install and test residential copper and PEX water lines'}}),
        9: ('get_program_completion_status', {'program_id': 32, 'apprentice_id': 552}),
    }
    if idx not in specs:
        return None
    tool, args = specs[idx]
    return {'calls': [{'tool': tool, 'arguments': args}], 'source': __file__,
            'prefix_only': idx == 9, 'scope': 'reviewed task-literal regression probe; true encoded as contract integer 1'}


def tool_results(trace):
    return [json.loads(r['tool_response']['content']) for r in trace['trajectory']
            if any(c['name'] == 'call_tool' for c in r.get('tool_calls', []))]


def business_check(task, before, after, trace):
    """A mismatch is evidence, not an automatic claim of no alternative solution."""
    if trace.get('failure'):
        return {'status': 'not_checked_due_to_runtime_failure'}
    if task['scenario'] != 'education_training_management_1':
        return {'status': 'not_covered_by_independent_template'}
    idx = task['task_idx']
    got = tool_results(trace)
    if not got:
        return {'status': 'not_covered_by_independent_template'}
    changes = delta(before, after)
    if idx in (2, 3, 6):
        table = {2: 'competency_records', 3: 'work_log_entries', 6: 'class_evaluations'}[idx]
        added = changes.get(table, {}).get('added', [])
        expected = curated_recipe(task)['calls'][0]['arguments']['body'].copy()
        expected['apprentice_id'] = 552
        if idx == 6:
            expected['class_id'] = 88
        passed = any(all(r.get(k) == v for k, v in expected.items()) for r in added)
        return {'status': 'passed' if passed else 'mismatch', 'expected_new_row_fields': expected,
                'added_rows': added, 'table': table, 'actual_response': got[0]}
    if idx == 8:
        expected = curated_recipe(task)['calls'][0]['arguments']['body']
        target = [r for r in after['competencies'] if r['id'] == 340]
        passed = len(target) == 1 and all(target[0][k] == v for k, v in expected.items())
        return {'status': 'passed' if passed else 'mismatch', 'expected_fields': expected, 'actual': target}
    if idx == 7:
        hours = [r for r in before['work_log_entries'] if r['apprentice_id'] == 552 and '2025-01-01' <= r['work_date'][:10] <= '2025-03-31']
        comps = [r for r in before['competency_records'] if r['apprentice_id'] == 552 and r['rating'] >= 3 and '2025-01-01' <= r['record_date'][:10] <= '2025-03-31']
        evs = [r for r in before['class_evaluations'] if r['apprentice_id'] == 552 and '2025-01-01' <= r['evaluation_date'][:10] <= '2025-03-31']
        expected = {'total_work_hours': sum(r['hours_worked'] for r in hours), 'completed_competencies_count': len({r['competency_id'] for r in comps}),
                    'average_evaluation_score': sum(r['score'] for r in evs)/len(evs) if evs else None}
        return {'status': 'passed' if all(got[0].get(k) == v for k, v in expected.items()) else 'mismatch',
                'expected': expected, 'actual': got[0], 'rows': {'hours': hours, 'competencies': comps, 'evaluations': evs}}
    if idx == 9:
        hours = sum(r['hours_worked'] for r in before['work_log_entries'] if r['apprentice_id'] == 552)
        program = next(r for r in before['programs'] if r['id'] == 32)
        required = {r['competency_id'] for r in before['program_required_competencies'] if r['program_id'] == 32 and r['is_mandatory']}
        completed = {r['competency_id'] for r in before['competency_records'] if r['apprentice_id'] == 552 and r['rating'] >= 3}
        classes = {r['class_id'] for r in before['program_required_classes'] if r['program_id'] == 32 and r['is_mandatory']}
        passed = {r['class_id'] for r in before['class_evaluations'] if r['apprentice_id'] == 552 and r['pass_fail'].lower() == 'pass'}
        expected = {'total_hours_on_job': hours, 'is_program_completed': hours >= program['required_hours_on_job'] and required <= completed and classes <= passed}
        return {'status': 'passed' if all(got[0].get(k) == v for k, v in expected.items()) else 'mismatch', 'expected': expected, 'actual': got[0],
                'requirements': {'program': program, 'required_competencies': sorted(required), 'completed_competencies': sorted(completed), 'required_classes': sorted(classes), 'passed_classes': sorted(passed)}}
    return {'status': 'not_covered_by_independent_template'}
