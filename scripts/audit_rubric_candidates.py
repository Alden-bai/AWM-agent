"""Offline reproductions of candidate/source code issues; never reviews rubrics."""
import json
from pathlib import Path

from rubric_candidates import extract_candidates, prepare_input
from rubric_pilot import ROOT, write_json


def contract_input(contract):
    task = 'Create a note.'
    return dict(task_id='code-audit', task=task, sources=[
        dict(id='task', kind='task', text=task),
        dict(id='contract:create', kind='public_contract', text=json.dumps(contract)),
    ])


def audit():
    findings = []
    try:
        prepare_input(contract_input(dict(description='创建笔记。')))
    except ValueError as exc:
        findings.append(dict(id='escaped_description', reproduced=True, observed=str(exc)))
    else:
        findings.append(dict(id='escaped_description', reproduced=False))

    view, _ = prepare_input(contract_input(dict(request_params={
        'example': dict(type='string', description='Required user value.')
    })))
    params = view['public_contracts'][0]['contract']['request_params']
    findings.append(dict(id='real_parameter_named_example', reproduced='example' not in params,
                         observed=params))

    _, pack = prepare_input(contract_input(dict(summary='Same text.', description='Same text.')))
    refs = [r for k, r in pack['reference_registry'].items() if k.startswith('K')]
    findings.append(dict(id='duplicate_description_offsets',
                         reproduced=refs[0]['start'] == refs[1]['start'], observed=refs))

    task = 'Create “A, B and C”; then save.'
    quotes = [c['quote'] for c in extract_candidates(task)['candidates']]
    findings.append(dict(id='curly_quote_split',
                         reproduced=not any('“A, B and C”' in q for q in quotes), observed=quotes))
    write_json(ROOT/'runs/rubric_candidate_code_audit_20261024/edge_cases.json',
               dict(model_calls=0, semantic_review_performed=False, findings=findings))
    print(json.dumps(dict(reproduced=[f['id'] for f in findings if f['reproduced']], model_calls=0)))


if __name__ == '__main__':
    audit()
