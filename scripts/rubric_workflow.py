"""Source-preserving normalization and fail-closed checks; no network calls.

The model still emits the existing five business fields. Plans and semantic
test results are separate artifacts, never injected into the rubric schema.
"""
import copy
import hashlib
import json
import re

from rubric_qwen_v10 import validate_contract


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize(inp):
    """Move examples out of contracts; preserve every other property and DDL.

    Quote entries refer to the *original serialized text*, not a reformatted
    view. Escaped JSON string contents therefore remain valid original spans.
    """
    out = dict(task_id=inp['task_id'], task=inp['task'], public_contracts=[], schemas=[],
               citation_catalog=[], non_normative_examples=[],
               tool_inventory=copy.deepcopy(inp.get('tool_inventory', [])),
               evidence_context=dict(types=inp.get('available_evidence_types', []),
                                     note=inp.get('evidence_note', '')),
               quality_policy=inp.get('quality_policy'))
    audit = dict(version='source-normalization-v1', input_digest=digest(inp), sources=[])
    seen = set()

    def quote(source, text, pointer):
        if not text or text not in source['text']:
            raise ValueError('quote_not_original:' + source['id'])
        start = source['text'].index(text)
        out['citation_catalog'].append(dict(source_id=source['id'], quote=text, pointer=pointer))
        return dict(quote=text, start=start, end=start+len(text), pointer=pointer)

    for s in inp['sources']:
        if s['id'] in seen:
            raise ValueError('duplicate_source_id')
        seen.add(s['id'])
        record = dict(source_id=s['id'], sha256=hashlib.sha256(s['text'].encode()).hexdigest(), spans=[])
        if s['kind'] == 'public_contract':
            raw = json.loads(s['text'])

            def walk(node, path=''):
                if isinstance(node, dict):
                    result = {}
                    for k, v in node.items():
                        pointer = path + '/' + k.replace('~', '~0').replace('/', '~1')
                        if k in ('example', 'examples'):
                            out['non_normative_examples'].append(dict(source_id=s['id'], pointer=pointer, value=v))
                        else:
                            result[k] = walk(v, pointer)
                            if k in ('description', 'summary') and isinstance(v, str) and v:
                                # Original JSON uses ensure_ascii=False in frozen inputs.
                                encoded = json.dumps(v, ensure_ascii=False)[1:-1]
                                record['spans'].append(quote(s, encoded, pointer))
                    return result
                if isinstance(node, list):
                    return [walk(v, path+'/'+str(i)) for i, v in enumerate(node)]
                return node

            cleaned = walk(raw)
            out['public_contracts'].append(dict(source_id=s['id'], contract=cleaned))
            # Reconstruct to prove no non-example data was dropped or changed.
            restored = copy.deepcopy(cleaned)
            for e in out['non_normative_examples']:
                if e['source_id'] != s['id']:
                    continue
                keys = [k.replace('~1', '/').replace('~0', '~') for k in e['pointer'].split('/')[1:]]
                dest = restored
                for k in keys[:-1]:
                    dest = dest[int(k)] if isinstance(dest, list) else dest[k]
                dest[keys[-1]] = copy.deepcopy(e['value'])
            if restored != raw:
                raise ValueError('normalization_lost_source_information')
        elif s['kind'] == 'schema':
            out['schemas'].append(dict(source_id=s['id'], ddl=s['text']))
            for line in s['text'].splitlines():
                if line.strip():
                    record['spans'].append(quote(s, line.strip(), '/ddl'))
        elif s['kind'] == 'task':
            record['spans'].append(quote(s, s['text'], '/task'))
        else:
            raise ValueError('unsupported_source_kind:' + s['kind'])
        audit['sources'].append(record)
    if 'task' not in seen or next(s['text'] for s in inp['sources'] if s['id'] == 'task') != inp['task']:
        raise ValueError('task_source_mismatch')
    audit['normalized_digest'] = digest(out)
    audit['examples_separated'] = len(out['non_normative_examples'])
    return out, audit


def refs_valid(refs, inp):
    sources = {s['id']: s['text'] for s in inp['sources']}
    return isinstance(refs, list) and bool(refs) and all(
        isinstance(r, dict) and set(r) == {'source_id', 'quote'} and
        isinstance(r.get('source_id'), str) and r['source_id'] in sources and
        isinstance(r.get('quote'), str) and bool(r['quote']) and r['quote'] in sources[r['source_id']]
        for r in refs)


def validate_plan(out, inp):
    if not isinstance(out, dict) or set(out) != {'task_id', 'obligations', 'ambiguities', 'cases'}:
        return ['invalid_plan_root']
    errors = []
    if out['task_id'] != inp['task_id']:
        errors.append('wrong_task_id')
    obligations = out['obligations']
    if not isinstance(obligations, list) or not obligations:
        return errors + ['empty_obligations']
    ids = []
    for o in obligations:
        if not isinstance(o, dict) or set(o) != {'id', 'task_quote', 'requirement', 'binding', 'temporal_scope', 'source_refs'}:
            errors.append('invalid_obligation')
            continue
        if any(not isinstance(o[k], str) or not o[k].strip() for k in ('id','task_quote','requirement','binding','temporal_scope')):
            errors.append('empty_obligation_field')
            continue
        ids.append(o['id'])
        if o['task_quote'] not in inp['task'] or not refs_valid(o['source_refs'], inp):
            errors.append('invalid_obligation_source:' + o['id'])
    if len(ids) != len(set(ids)):
        errors.append('duplicate_obligations')
    idset = set(ids)

    def valid_ids(values):
        return isinstance(values, list) and bool(values) and all(isinstance(x, str) and x in idset for x in values) and len(values) == len(set(values))

    if not isinstance(out['ambiguities'], list):
        errors.append('invalid_ambiguities')
    else:
        for a in out['ambiguities']:
            if not isinstance(a, dict) or set(a) != {'issue','obligation_ids','needed'} or not valid_ids(a.get('obligation_ids')) or any(not isinstance(a.get(k),str) or not a[k].strip() for k in ('issue','needed')):
                errors.append('invalid_ambiguity')
    if not isinstance(out['cases'], list) or not out['cases']:
        return errors + ['empty_cases']
    caseids, kinds, negatives = [], set(), set()
    expected = dict(positive='satisfied', alternative='satisfied', negative='violated', missing_evidence='unknown')
    for c in out['cases']:
        if not isinstance(c, dict) or set(c) != {'id','kind','obligation_ids','scenario','expected','reason'}:
            errors.append('invalid_case')
            continue
        if any(not isinstance(c.get(k), str) or not c[k].strip() for k in ('id','kind','scenario','expected','reason')) or not valid_ids(c.get('obligation_ids')):
            errors.append('invalid_case_fields')
            continue
        caseids.append(c['id'])
        kinds.add(c['kind'])
        if expected.get(c['kind']) != c['expected']:
            errors.append('wrong_case_expected:' + c['id'])
        if c['kind'] == 'negative':
            negatives.update(c['obligation_ids'])
        if c['kind'] == 'positive' and set(c['obligation_ids']) != idset:
            errors.append('positive_not_complete:' + c['id'])
    if len(caseids) != len(set(caseids)):
        errors.append('duplicate_cases')
    if set(expected) - kinds:
        errors.append('missing_case_kinds')
    if negatives != idset:
        errors.append('missing_negative_coverage')
    return errors


def generator_input(normalized, plan):
    # Cases and their expected labels are intentionally withheld from generation.
    return dict(normalized, obligation_plan={k: copy.deepcopy(plan[k]) for k in ('obligations','ambiguities')})


def validate_rubric(out, inp, plan):
    try:
        errors = validate_contract(out, inp)
    except (TypeError, KeyError, AttributeError):
        return ['malformed_rubric']
    if not isinstance(out, dict):
        return errors
    covered = set()
    for c in out.get('coverage', []) if isinstance(out.get('coverage'), list) else []:
        if isinstance(c, dict) and isinstance(c.get('interpretation'), str):
            covered.update(re.findall(r'\[(O\d+)\]', c['interpretation']))
    expected = {o['id'] for o in plan['obligations']}
    errors += ['unmapped_obligation:'+x for x in sorted(expected-covered)]
    errors += ['unknown_obligation:'+x for x in sorted(covered-expected)]
    return errors


def score_review(review, plan, rubric):
    """Check exact case/obligation coverage; derive, never trust, pass counts.

    These are model-simulated semantic checks, not execution of a real checker.
    Invalid cases and ambiguous predictions remain unresolved, never passed.
    """
    errors = []
    if not isinstance(review, dict) or set(review) != {'task_id','obligation_checks','case_results','findings'}:
        return dict(errors=['invalid_review_root'], semantic_candidate=False, cases=[])
    if review['task_id'] != plan['task_id']:
        errors.append('wrong_task_id')
    itemids = {r['id'] for r in rubric.get('rubric', []) if isinstance(r, dict) and isinstance(r.get('id'), str)}
    obligations = {o['id'] for o in plan['obligations']}
    cases = {c['id']: c for c in plan['cases']}

    def rows(key, idkey, expected, fields):
        result = {}
        vals = review.get(key)
        if not isinstance(vals, list):
            errors.append('invalid_'+key)
            return result
        for r in vals:
            if not isinstance(r, dict) or set(r) != fields or not isinstance(r.get(idkey), str):
                errors.append('invalid_'+key+'_row')
                continue
            rid = r[idkey]
            if rid in result or rid not in expected:
                errors.append('duplicate_or_unknown_'+idkey)
            result[rid] = r
            if not isinstance(r.get('reason'), str) or not r['reason'].strip():
                errors.append('empty_review_reason')
            if not isinstance(r.get('item_ids'), list) or any(not isinstance(x,str) or x not in itemids for x in r['item_ids']):
                errors.append('invalid_review_item_refs')
        if set(result) != set(expected):
            errors.append('incomplete_'+key)
        return result

    oc = rows('obligation_checks','obligation_id',obligations,{'obligation_id','item_ids','status','reason'})
    cr = rows('case_results','case_id',cases,{'case_id','case_valid','predicted','item_ids','reason'})
    for r in oc.values():
        if r['status'] not in ('covered','missing','incorrect','ambiguous') or (r['status']=='covered' and not r['item_ids']):
            errors.append('invalid_obligation_verdict')
    scored = []
    for cid, r in cr.items():
        if cid not in cases:
            continue
        if type(r['case_valid']) is not bool or r['predicted'] not in ('satisfied','violated','unknown','ambiguous'):
            errors.append('invalid_case_verdict')
        c = cases[cid]
        scored.append(dict(case_id=cid, kind=c['kind'], expected=c['expected'], predicted=r['predicted'],
                           case_valid=r['case_valid'], passed=r['case_valid'] is True and r['predicted']==c['expected']))
    findings = review.get('findings')
    if not isinstance(findings, list):
        errors.append('invalid_findings')
    else:
        for f in findings:
            if not isinstance(f, dict) or set(f) != {'code','item_ids','issue','counterexample'} or any(not isinstance(f.get(k), str) or not f[k].strip() for k in ('code','issue','counterexample')) or not isinstance(f.get('item_ids'),list) or any(not isinstance(i,str) or i not in itemids for i in f['item_ids']):
                errors.append('invalid_finding')
    candidate = not errors and not findings and all(r['status']=='covered' for r in oc.values()) and all(r['passed'] for r in scored)
    return dict(errors=errors, semantic_candidate=candidate, cases=scored,
                cases_passed=sum(r['passed'] for r in scored), cases_total=len(cases),
                invalid_cases=sum(r['case_valid'] is not True for r in scored),
                limitations='Model simulation of hypothetical cases; independent source review still required; no runtime accuracy measured.')
