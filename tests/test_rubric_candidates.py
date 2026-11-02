import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rubric_candidates import extract_candidates,prepare_input,compile_rubric,validate_selection,evaluate_scores,DIMENSIONS
from rubric_qwen_deepseek_v12 import request,reservation,PilotBudget,THINKING
from rubric_pilot import APIConfig
from harness_awm.backend import BudgetExceeded
from harness_awm.protocol import estimate_tokens


def fixture():
    task='Create a note titled Alpha.'
    inp=dict(task_id='t',task=task,sources=[dict(id='task',kind='task',text=task),
        dict(id='contract:create',kind='public_contract',text=json.dumps(dict(description='Create a "new" note.',
            request_params={'title':{'default':'Untitled','example':'Alpha','description':'Note title.'}})))],quality_policy=None)
    view,pack=prepare_input(inp)
    out=dict(task_id='t',selection=[dict(candidate_id='C01',action='keep',item_ids=['R01'],reason='题面要求')],
        items=[dict(id='R01',candidate_ids=['C01'],contract_ref_ids=['K0001'],requirement_level='required',
            requirement='本次创建标题Alpha的笔记',gate='完整前后快照可识别新增集合C，空集也可判断',predicate="存在n∈C且title='Alpha'",
            evidence=[dict(description='笔记初末态ID与title',source_id=None)],method='deterministic')],unresolved=[])
    judge=dict(task_id='t',dimensions=[dict(name=n,score=2,reason='来源支持，边界明确',findings=[dict(item_ids=['R01'],candidate_ids=['C01'],problem='未发现阻断',counterexample='完整证据无新增应违反')]) for n in DIMENSIONS])
    return inp,view,pack,out,judge


class CandidateTests(unittest.TestCase):
    def test_quotes_arrays_numbers_and_unicode_are_preserved(self):
        task="新增备注 'A, B and C.'，然后保存。 Set tags ['x', 'y'] and limit 1,000 characters, then return JSON."
        p=extract_candidates(task)
        covered=set()
        for c in p['candidates']:
            self.assertEqual(task[c['start']:c['end']],c['quote'])
            covered.update(range(c['start'],c['end']))
        self.assertTrue(all(ch.isspace() or i in covered for i,ch in enumerate(task)))
        self.assertTrue(any("'A, B and C.'" in c['quote'] for c in p['candidates']))
        self.assertTrue(any("['x', 'y']" in c['quote'] for c in p['candidates']))
        self.assertTrue(any('1,000' in c['quote'] for c in p['candidates']))

    def test_repeated_clauses_have_distinct_offsets(self):
        p=extract_candidates('Create Alpha. Create Alpha.')
        self.assertEqual(len(p['candidates']),2)
        self.assertNotEqual(p['candidates'][0]['start'],p['candidates'][1]['start'])
        self.assertEqual(p['candidates'][0]['quote'],p['candidates'][1]['quote'])

    def test_contract_defaults_kept_examples_not_sent(self):
        inp,view,pack,*_=fixture()
        contract=view['public_contracts'][0]['contract']
        self.assertEqual(contract['request_params']['title']['default'],'Untitled')
        self.assertNotIn('example',contract['request_params']['title'])
        self.assertEqual(pack['excluded_examples'][0]['value'],'Alpha')
        self.assertTrue(pack['contracts_roundtrip_verified'])

    def test_compile_original_quotes_and_exact_five_fields(self):
        inp,view,pack,out,_=fixture()
        rubric=compile_rubric(out,inp,pack);r=rubric['rubric'][0]
        self.assertEqual(set(r),{'id','criterion','sources','requirement_level','evidence','decision'})
        sources={s['id']:s['text'] for s in inp['sources']}
        for ref in r['sources']: self.assertIn(ref['quote'],sources[ref['source_id']])
        self.assertEqual(r['decision']['unknown_when'],'本条G不成立')
        self.assertEqual(rubric['coverage'][0]['quote'],inp['task'])

    def test_no_unbound_or_silently_dropped_candidate(self):
        inp,_,pack,out,_=fixture()
        for fault in ('omit','duplicate','unknown_ref','inconsistent','background','ambiguity'):
            o=copy.deepcopy(out)
            if fault=='omit':o['selection']=[]
            if fault=='duplicate':o['selection'].append(o['selection'][0])
            if fault=='unknown_ref':o['items'][0]['contract_ref_ids']=['K9999']
            if fault=='inconsistent':o['selection'][0]['item_ids']=[]
            if fault=='background':o['selection'][0]['action']='background'
            if fault=='ambiguity':o['selection'][0]['action']='ambiguous'
            self.assertTrue(validate_selection(o,inp,pack),fault)
            with self.assertRaises(ValueError):compile_rubric(o,inp,pack)

    def test_tampered_source_cannot_compile(self):
        inp,_,pack,out,_=fixture();pack['reference_registry']['C01']['start']=1
        with self.assertRaises(ValueError):compile_rubric(out,inp,pack)

    def test_explicit_preference_can_be_quality_without_external_policy(self):
        inp,_,_,out,_=fixture();inp['task']='Preferably create a note.';inp['sources'][0]['text']=inp['task']
        _,pack=prepare_input(inp);out['items'][0]['requirement_level']='quality'
        self.assertEqual(compile_rubric(out,inp,pack)['rubric'][0]['requirement_level'],'quality')

    def test_zero_rejects_rubric_but_preserves_task_and_one_requires_review(self):
        *_,judge=fixture()
        def result(x):return evaluate_scores(x,'t',{'R01'},{'C01'})
        self.assertEqual(result(judge)['status'],'eligible')
        judge['dimensions'][0]['score']=1
        self.assertEqual(result(judge)['status'],'review')
        judge['dimensions'][1]['score']=0
        self.assertEqual(result(judge)['status'],'rejected')
        self.assertTrue(result(judge)['task_retained'])

    def test_judge_malformed_scores_never_eligible(self):
        *_,judge=fixture()
        for fault in ('missing','duplicate','bool','out_of_range','fake_ref','no_reason','no_finding','wrong_task'):
            j=copy.deepcopy(judge)
            if fault=='missing':j['dimensions'].pop()
            if fault=='duplicate':j['dimensions'].append(j['dimensions'][0])
            if fault=='bool':j['dimensions'][0]['score']=True
            if fault=='out_of_range':j['dimensions'][0]['score']=3
            if fault=='fake_ref':j['dimensions'][0]['findings'][0]['item_ids']=['R99']
            if fault=='no_reason':j['dimensions'][0]['reason']=''
            if fault=='no_finding':j['dimensions'][0]['findings']=[]
            if fault=='wrong_task':j['task_id']='other'
            r=evaluate_scores(j,'t',{'R01'},{'C01'})
            self.assertEqual(r['status'],'review',fault);self.assertTrue(r['errors'])

    def test_provider_budget_and_fresh_context(self):
        for stage in ('qwen','deepseek'):
            req=request(stage,{'task':'hello'},APIConfig(model=stage),'Return JSON')
            self.assertEqual([m['role'] for m in req['messages']],['system','user'])
            expected=estimate_tokens(req['messages'])+req['max_tokens']+512+(THINKING if stage=='qwen' else 0)
            self.assertEqual(reservation(stage,req),expected)
            with tempfile.TemporaryDirectory() as d:
                b=PilotBudget(Path(d)/'ledger.sqlite',1,expected-1)
                with self.assertRaises(BudgetExceeded):b.reserve('k','v12_'+stage,req['messages'],req['max_tokens'])
                self.assertEqual(b.summary()['physical_requests'],0)


if __name__=='__main__':unittest.main()
