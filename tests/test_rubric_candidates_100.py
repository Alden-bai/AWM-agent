"""Required regressions for source fidelity; no semantic model evaluation."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rubric_candidates_100 import prepare_input, extract_candidates, json_spans, validate_selection, evaluate


def inp(contract):
    task = 'Create a note.'
    return dict(task_id='test:0', task=task, sources=[dict(id='task', kind='task', text=task),
                dict(id='contract:create', kind='public_contract', text=json.dumps(contract))])


class SourceRegressionTests(unittest.TestCase):
    def test_original_unicode_json_escape_and_duplicate_offsets(self):
        data = inp(dict(summary='创建笔记。', description='创建笔记。'))
        view, audit = prepare_input(data)
        refs = [r for k, r in audit['reference_registry'].items() if k.startswith('K')]
        self.assertNotEqual(refs[0]['start'], refs[1]['start'])
        _, spans = json_spans(data['sources'][1]['text'])
        for r in refs:
            self.assertEqual(r['start'], spans[r['pointer']]['start'])
            self.assertEqual(data['sources'][1]['text'][r['start']:r['end']], r['quote'])
            self.assertEqual(json.loads('"'+r['quote']+'"'), '创建笔记。')
        self.assertEqual(view['public_contracts'][0]['contract']['summary']['text'], '创建笔记。')

    def test_business_example_fields_preserved_metadata_only_removed(self):
        contract = dict(request_params={k: dict(type='string', description='Required value.', example='sample') for k in ('example', 'examples', 'description', 'type')},
                        response=dict(properties={'example': dict(type='object', description='Business object', example={'id': 1})}))
        view, audit = prepare_input(inp(contract))
        params = view['public_contracts'][0]['contract']['request_params']
        self.assertEqual(set(params), set(contract['request_params']))
        self.assertNotIn('example', params['example'])
        self.assertIn('example', view['public_contracts'][0]['contract']['response']['properties'])
        self.assertEqual(len(audit['excluded_examples']), 5)

    def test_ambiguous_examples_retained(self):
        view, audit = prepare_input(inp(dict(examples={'business': 'value'})))
        self.assertEqual(view['public_contracts'][0]['contract']['examples'], {'business': 'value'})
        self.assertEqual(audit['excluded_examples'], [])

    def test_nested_json_arrays_pointer_escaping(self):
        text = '{"a/b":[{"~description":"A\\nB"},{"~description":"A\\nB"}]}'
        value, spans = json_spans(text)
        self.assertEqual(value['a/b'][0]['~description'], 'A\nB')
        self.assertNotEqual(spans['/a~1b/0/~0description']['start'], spans['/a~1b/1/~0description']['start'])
        with self.assertRaises(ValueError):
            json_spans('{"x":1,"x":2}')

    def test_protected_quotes_apostrophes_and_nested_brackets(self):
        for protected in ('“A, B and C”', "'Item defective or doesn't work'", '‘Don’t, split and save’', '(A, [B and C])', '"A, B and C"'):
            task = 'Create '+protected+'; then save.'
            candidates = extract_candidates(task)['candidates']
            self.assertTrue(any(protected in c['quote'] for c in candidates), protected)
            for c in candidates:
                self.assertEqual(task[c['start']:c['end']], c['quote'])

    def test_malformed_selection_returns_paths(self):
        data = inp({})
        _, pack = prepare_input(data)
        out = dict(task_id='test:0', selection=[], items=[dict(id='R01', candidate_ids=['C01'], contract_ref_ids=[],
                    requirement_level='required', requirement='Create note', gate='Evidence', predicate='Exists',
                    evidence=[dict(description='Record', source_id='K0001')], method='deterministic')], unresolved=[])
        errors = validate_selection(out, data, pack)
        self.assertTrue(any(e['path'] == '/items/0/evidence/0/source_id' and e['actual'] == 'K0001' for e in errors))
        self.assertTrue(validate_selection(dict(out, selection=[dict(candidate_id=[], action='keep', item_ids=[], reason='x')]), data, pack))

    def test_valid_one_scores_pass_and_zero_rejects(self):
        from rubric_candidates_100 import DIMENSIONS
        out = dict(task_id='test:0', dimensions=[dict(name=d, score=1, reason='Reason', findings=[dict(item_ids=['R01'], candidate_ids=[], problem='Boundary', counterexample='Example')]) for d in DIMENSIONS])
        self.assertEqual(evaluate(out, 'test:0', {'R01'}, {'C01'})['status'], 'passed')
        out['dimensions'][0]['score'] = 0
        self.assertEqual(evaluate(out, 'test:0', {'R01'}, {'C01'})['status'], 'rejected')
        out['dimensions'][0]['score'] = True
        self.assertEqual(evaluate(out, 'test:0', {'R01'}, {'C01'})['status'], 'invalid_judge')


if __name__ == '__main__':
    unittest.main()
