"""Source-preserving, deterministic preparation for the Luna 100 pilot.

Historical candidate code is reused only for structural validation/assembly.
This module does not make semantic decisions or model calls.
"""
import copy
import json
import re

import rubric_candidates as legacy
from jsonschema import Draft202012Validator

sha = legacy.sha
DIMENSIONS = legacy.DIMENSIONS


def json_spans(text):
    """Parse actual JSON tokens, indexing value spans by RFC6901 pointer.

    String spans exclude quotes and retain the original escaping. Duplicate
    object keys are rejected instead of silently binding the wrong occurrence.
    """
    decoder = json.JSONDecoder()
    spans = {}

    def ws(i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    def parse(i, pointer):
        i = ws(i)
        start = i
        if text[i] == '{':
            result = {}
            i = ws(i + 1)
            while text[i] != '}':
                key, end = decoder.raw_decode(text, i)
                if not isinstance(key, str) or key in result:
                    raise ValueError('duplicate_or_invalid_json_key:' + pointer)
                i = ws(end)
                if text[i] != ':':
                    raise ValueError('expected_colon')
                loc = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
                result[key], i = parse(i + 1, loc)
                i = ws(i)
                if text[i] == '}':
                    break
                if text[i] != ',':
                    raise ValueError('expected_comma')
                i = ws(i + 1)
                if text[i] == '}':
                    raise ValueError('trailing_comma')
            value, end = result, i + 1
        elif text[i] == '[':
            result = []
            i = ws(i + 1)
            while text[i] != ']':
                item, i = parse(i, pointer + '/' + str(len(result)))
                result.append(item)
                i = ws(i)
                if text[i] == ']':
                    break
                if text[i] != ',':
                    raise ValueError('expected_comma')
                i = ws(i + 1)
                if text[i] == ']':
                    raise ValueError('trailing_comma')
            value, end = result, i + 1
        else:
            value, end = decoder.raw_decode(text, i)
        spans[pointer] = dict(start=start + (1 if isinstance(value, str) else 0),
                              end=end - (1 if isinstance(value, str) else 0),
                              token_start=start, token_end=end)
        return value, end

    value, end = parse(0, '')
    if ws(end) != len(text):
        raise ValueError('trailing_json')
    return value, spans


def extract_candidates(task):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('empty_task')
    boundaries = {0, len(task)}
    quote = None
    stack = []
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’', '«': '»', '「': '」', '『': '』'}
    i = 0
    while i < len(task):
        c = task[i]
        apostrophe = c in ("'", '’') and i > 0 and i + 1 < len(task) and task[i - 1].isalnum() and task[i + 1].isalnum()
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote and not apostrophe:
                quote = None
        elif c in pairs and not apostrophe:
            quote = pairs[c]
        elif c in '([{':
            stack.append(c)
        elif c in ')]}':
            if stack and stack[-1] == {')': '(', ']': '[', '}': '{'}[c]:
                stack.pop()
        elif not stack:
            if c in ',;\n' or (c in '.!?' and (i + 1 == len(task) or task[i + 1].isspace())):
                if not (c == ',' and i and i + 1 < len(task) and task[i - 1].isdigit() and task[i + 1].isdigit()):
                    boundaries.add(i + 1)
            if i and task[i - 1].isspace() and re.match(r'(?:and|then|if|unless|but)\b', task[i:], re.I):
                boundaries.add(i)
        i += 1
    candidates = []
    points = sorted(boundaries)
    for a, b in zip(points, points[1:]):
        while a < b and task[a].isspace():
            a += 1
        while b > a and task[b - 1].isspace():
            b -= 1
        if a == b:
            continue
        q = task[a:b]
        if candidates and not re.search(r'\w', q):
            candidates[-1].update(end=b, quote=task[candidates[-1]['start']:b])
            continue
        candidates.append(dict(candidate_id=f'C{len(candidates)+1:02d}', source_id='task', start=a, end=b,
                               quote=q, signals=[dict(text=m.group(), start=a+m.start(), end=a+m.end()) for m in legacy.SIGNALS.finditer(q)]))
    covered = {i for c in candidates for i in range(c['start'], c['end'])}
    if any(not c.isspace() and i not in covered for i, c in enumerate(task)):
        raise ValueError('uncovered_task_characters')
    return dict(version='candidate-spans-100-v1', task_sha256=sha(task), offset_unit='unicode_codepoint',
                complete_nonwhitespace_coverage=True, candidates=candidates)


def prepare_input(inp):
    pack = extract_candidates(inp['task'])
    registry = {c['candidate_id']: {k: c[k] for k in ('source_id', 'quote', 'start', 'end')} for c in pack['candidates']}
    view = dict(task_id=inp['task_id'], task=inp['task'], candidates=pack['candidates'], public_contracts=[], schemas=[],
                tool_inventory=inp.get('tool_inventory', []), quality_policy=inp.get('quality_policy'),
                evidence_context=dict(types=inp.get('available_evidence_types', []), note=inp.get('evidence_note', '')))
    excluded, seen = [], set()
    ref_count = 0
    for source in inp['sources']:
        sid, text = source['id'], source['text']
        if sid in seen:
            raise ValueError('duplicate_source_id')
        seen.add(sid)
        if source['kind'] == 'public_contract':
            original, spans = json_spans(text)

            def walk(node, pointer=''):
                nonlocal ref_count
                if isinstance(node, dict):
                    result = {}
                    # Only scalar schema-descriptor examples are confirmed
                    # metadata. Field-name maps (including example/examples)
                    # have a dict-valued type or no type and remain intact.
                    descriptor = isinstance(node.get('type'), str) and ('description' in node or 'param_type' in node)
                    for k, value in node.items():
                        loc = pointer + '/' + k.replace('~', '~0').replace('/', '~1')
                        if k in ('example', 'examples') and descriptor:
                            excluded.append(dict(source_id=sid, pointer=loc, value=value, reason='schema_descriptor_example_metadata'))
                        elif k in ('summary', 'description') and isinstance(value, str) and value:
                            ref_count += 1
                            ref = f'K{ref_count:04d}'
                            span = spans[loc]
                            a, b = span['start'], span['end']
                            registry[ref] = dict(source_id=sid, quote=text[a:b], start=a, end=b, pointer=loc)
                            if json.loads('"' + text[a:b] + '"') != value:
                                raise ValueError('json_span_decode_mismatch')
                            result[k] = dict(text=value, ref_id=ref)
                        else:
                            result[k] = walk(value, loc)
                    return result
                if isinstance(node, list):
                    return [walk(v, pointer+'/'+str(i)) for i, v in enumerate(node)]
                return node

            contract = walk(original)

            def restore(node):
                if isinstance(node, dict):
                    if set(node) == {'text', 'ref_id'} and node['ref_id'] in registry:
                        return node['text']
                    return {k: restore(v) for k, v in node.items()}
                if isinstance(node, list):
                    return [restore(v) for v in node]
                return node

            restored = restore(contract)
            for e in excluded:
                if e['source_id'] != sid:
                    continue
                keys = [k.replace('~1', '/').replace('~0', '~') for k in e['pointer'].split('/')[1:]]
                target = restored
                for k in keys[:-1]:
                    target = target[int(k)] if isinstance(target, list) else target[k]
                target[keys[-1]] = e['value']
            if restored != original:
                raise ValueError('contract_roundtrip_failed')
            view['public_contracts'].append(dict(source_id=sid, contract=contract))
        elif source['kind'] == 'schema':
            view['schemas'].append(dict(source_id=sid, ddl=text))
        elif source['kind'] == 'task':
            if sid != 'task' or text != inp['task']:
                raise ValueError('wrong_task_source')
        else:
            raise ValueError('unsupported_source_kind')
    if 'task' not in seen:
        raise ValueError('missing_task_source')
    return view, dict(**pack, reference_registry=registry, excluded_examples=excluded,
                      source_hashes={s['id']: sha(s['text']) for s in inp['sources']}, contracts_roundtrip_verified=True,
                      compression='outer_json_whitespace_only; confirmed_scalar_schema_example_metadata_removed; every_other_attribute_and_all_DDL_preserved')


def selection_schema(inp, pack):
    string = dict(type='string', minLength=1)

    def array(item, minimum=0):
        return dict(type='array', items=item, minItems=minimum, uniqueItems=True)

    def obj(props):
        return dict(type='object', properties=props, required=list(props), additionalProperties=False)

    cid = dict(enum=[c['candidate_id'] for c in pack['candidates']])
    rid = dict(type='string', pattern=r'^R[0-9]+$')
    refs = dict(enum=[k for k in pack['reference_registry'] if k.startswith('K')])
    source = dict(enum=[s['id'] for s in inp['sources']] + [None])
    return obj(dict(task_id=dict(const=inp['task_id']),
                    selection=array(obj(dict(candidate_id=cid, action=dict(enum=['keep', 'merge', 'background', 'ambiguous']), item_ids=array(rid), reason=string)), 1),
                    items=array(obj(dict(id=rid, candidate_ids=array(cid, 1), contract_ref_ids=array(refs),
                                         requirement_level=dict(enum=['required', 'quality']), requirement=string, gate=string, predicate=string,
                                         evidence=array(obj(dict(description=string, source_id=source)), 1), method=dict(enum=['deterministic', 'semantic', 'hybrid']))), 1),
                    unresolved=array(obj(dict(issue=string, affected_items=array(rid, 1), needed=string)))))


def validate_selection(out, inp, pack):
    schema_errors = sorted(Draft202012Validator(selection_schema(inp, pack)).iter_errors(out), key=lambda e: str(list(e.path)))
    errors = [dict(path='/'+'/'.join(map(str, e.path)), actual=e.instance, expected=e.message,
                   legal_source_ids=[s['id'] for s in inp['sources']]) for e in schema_errors]
    if errors:
        return errors
    return [dict(path='/', actual=code, expected='unique IDs, complete bidirectional candidate mappings and valid unresolved references',
                 legal_source_ids=[s['id'] for s in inp['sources']]) for code in legacy.validate_selection(out, inp, pack)]


def compile_rubric(out, inp, pack):
    errors = validate_selection(out, inp, pack)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return legacy.compile_rubric(out, inp, pack)


def evaluate(out, task_id, item_ids, candidate_ids):
    try:
        result = legacy.evaluate_scores(out, task_id, item_ids, candidate_ids)
    except (TypeError, ValueError, KeyError) as exc:
        return dict(status='invalid_judge', errors=[type(exc).__name__], scores={}, zero_dimensions=[], task_retained=True)
    result['status'] = 'invalid_judge' if result['errors'] else ('rejected' if result['zero_dimensions'] else 'passed')
    return result
