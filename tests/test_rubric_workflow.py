"""Behavioral tests for normalization, leakage and fail-closed semantic gates."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rubric_workflow import normalize, generator_input, validate_plan, score_review


def fixture():
    inp=dict(task_id='t',task='Create a note.',sources=[dict(id='task',kind='task',text='Create a note.'),
        dict(id='contract:create',kind='public_contract',text=json.dumps(dict(description='Creates a "new" note.',
            response={'id':{'default':7,'example':99,'description':'New ID.'}}, alternatives=[{'example':3,'minimum':1}]))),
        dict(id='schema:notes',kind='schema',text='CREATE TABLE notes (id INTEGER PRIMARY KEY);')],quality_policy=None)
    plan=dict(task_id='t',obligations=[dict(id='O01',task_quote='Create a note.',requirement='本次创建笔记',
        binding='同一新增笔记',temporal_scope='本次',source_refs=[dict(source_id='task',quote='Create a note.')])],ambiguities=[],cases=[])
    for i,(kind,expected) in enumerate([('positive','satisfied'),('negative','violated'),('alternative','satisfied'),('missing_evidence','unknown')]):
        plan['cases'].append(dict(id=f'C{i}',kind=kind,obligation_ids=['O01'],scenario='假设完整证据或明确缺证据',expected=expected,reason='题面要求'))
    rubric=dict(rubric=[dict(id='R01')])
    review=dict(task_id='t',obligation_checks=[dict(obligation_id='O01',item_ids=['R01'],status='covered',reason='P要求本次创建')],
        case_results=[dict(case_id=c['id'],case_valid=True,predicted=c['expected'],item_ids=['R01'],reason='根据实际谓词推演') for c in plan['cases']],findings=[])
    return inp,plan,rubric,review


class WorkflowTests(unittest.TestCase):
    def test_preserve_defaults_and_original_escaped_quotes(self):
        inp,*_=fixture(); before=copy.deepcopy(inp)
        normalized,audit=normalize(inp)
        self.assertEqual(inp,before)
        contract=normalized['public_contracts'][0]['contract']
        self.assertEqual(contract['response']['id']['default'],7)
        self.assertNotIn('example',contract['response']['id'])
        self.assertEqual(audit['examples_separated'],2)
        self.assertEqual(contract['alternatives'],[{'minimum':1}])
        source={s['id']:s['text'] for s in inp['sources']}
        for c in normalized['citation_catalog']:
            self.assertIn(c['quote'],source[c['source_id']])

    def test_cases_withheld_from_generator(self):
        inp,plan,*_=fixture(); norm,_=normalize(inp)
        payload=generator_input(norm,plan)
        self.assertNotIn('cases',payload['obligation_plan'])
        self.assertNotIn('expected',json.dumps(payload))
        payload['obligation_plan']['obligations'][0]['requirement']='changed'
        self.assertNotEqual(plan['obligations'][0]['requirement'],'changed')

    def test_bad_sources_and_missing_negatives_rejected(self):
        inp,plan,*_=fixture()
        self.assertEqual(validate_plan(plan,inp),[])
        for fault in ('quote','negative','duplicate','expected','reference'):
            p=copy.deepcopy(plan)
            if fault=='quote': p['obligations'][0]['source_refs'][0]['quote']='invented'
            if fault=='negative': p['cases']=[c for c in p['cases'] if c['kind']!='negative']
            if fault=='duplicate': p['cases'].append(p['cases'][0])
            if fault=='expected': p['cases'][1]['expected']='satisfied'
            if fault=='reference': p['cases'][0]['obligation_ids']=['O99']
            self.assertTrue(validate_plan(p,inp),fault)

    def test_review_must_actually_check_every_case(self):
        _,p,r,v=fixture()
        self.assertTrue(score_review(v,p,r)['semantic_candidate'])
        for fault in ('missing','duplicate','wrong_prediction','invalid_case','ambiguous','missing_obligation','finding','string_bool'):
            w=copy.deepcopy(v)
            if fault=='missing': w['case_results'].pop()
            if fault=='duplicate': w['case_results'].append(w['case_results'][0])
            if fault=='wrong_prediction': w['case_results'][1]['predicted']='unknown'
            if fault=='invalid_case': w['case_results'][0]['case_valid']=False
            if fault=='ambiguous': w['case_results'][0]['predicted']='ambiguous'
            if fault=='missing_obligation': w['obligation_checks']=[]
            if fault=='finding': w['findings']=[dict(code='other',item_ids=['R01'],issue='遗漏',counterexample='完整证据未做')]
            if fault=='string_bool': w['case_results'][0]['case_valid']='true'
            self.assertFalse(score_review(w,p,r)['semantic_candidate'],fault)

    def test_malformed_inputs_fail_closed(self):
        inp,p,r,_=fixture()
        self.assertTrue(validate_plan({'task_id':'t'},inp))
        self.assertFalse(score_review([],p,r)['semantic_candidate'])


if __name__=='__main__': unittest.main()
