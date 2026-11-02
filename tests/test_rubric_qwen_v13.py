import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from rubric_qwen_v13 import evaluate, process_task, judge_payload, request, reservation, InvalidOutput
from rubric_pilot import APIConfig
from test_rubric_candidates import fixture


class GateTests(unittest.TestCase):
    def test_all_ones_pass_and_each_zero_rejects(self):
        inp,view,pack,out,judgment=fixture()
        for d in judgment['dimensions']:d['score']=1
        self.assertEqual(evaluate(judgment,'t',{'R01'},{'C01'})['status'],'passed')
        for index in range(5):
            changed=copy.deepcopy(judgment);changed['dimensions'][index]['score']=0
            self.assertEqual(evaluate(changed,'t',{'R01'},{'C01'})['status'],'rejected')

    def test_invalid_scores_never_pass(self):
        *_,judgment=fixture()
        for score in (True,3,'1'):
            changed=copy.deepcopy(judgment);changed['dimensions'][0]['score']=score
            self.assertEqual(evaluate(changed,'t',{'R01'},{'C01'})['status'],'invalid_judge')
        judgment['dimensions'].pop()
        self.assertEqual(evaluate(judgment,'t',{'R01'},{'C01'})['status'],'invalid_judge')

    def test_two_message_request_and_guard(self):
        _,view,*_=fixture()
        api=APIConfig(base_url='https://example.invalid',model='qwen3.5-flash',api_key_env='TEST_NO_NETWORK')
        req=request(view,api,'judge')
        self.assertEqual([m['role'] for m in req['messages']],['system','user'])
        self.assertLess(reservation(req),128000)
        self.assertGreater(reservation(request({'task':'x'*150000},api,'judge')),128000)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_pass_stops_without_repair(self):
        inp,view,pack,out,judge=fixture();calls=[]
        async def call(role,n,payload):
            calls.append((role,n));return copy.deepcopy(out if role=='generate' else judge)
        result=await process_task(inp,view,pack,call,lambda *a:None)
        self.assertEqual(result['accepted_revision'],0)
        self.assertEqual(calls,[('generate',0),('judge',0)])

    async def test_repair_sees_feedback_but_new_judge_never_sees_history(self):
        inp,view,pack,out,judge=fixture();calls=[];saved={}
        async def call(role,n,payload):
            calls.append((role,n))
            if role=='generate':
                if n==1:self.assertEqual(payload['revision_feedback']['judgment']['dimensions'][0]['score'],0)
                else:self.assertNotIn('revision_feedback',payload)
                return copy.deepcopy(out)
            self.assertEqual(payload,judge_payload(view,saved[n,'rubric.json']))
            for forbidden in ('previous_output','revision_feedback','reasoning_content','messages','previous_scores'):
                self.assertNotIn(forbidden,payload)
            j=copy.deepcopy(judge)
            for d in j['dimensions']:d['score']=1
            if n==0:j['dimensions'][0]['score']=0
            return j
        result=await process_task(inp,view,pack,call,lambda n,f,d:saved.__setitem__((n,f),d))
        self.assertEqual(result['accepted_revision'],1)
        self.assertEqual(calls,[('generate',0),('judge',0),('generate',1),('judge',1)])

    async def test_at_most_two_revisions(self):
        inp,view,pack,out,judge=fixture();calls=[];judge['dimensions'][0]['score']=0
        async def call(role,n,payload):calls.append((role,n));return copy.deepcopy(out if role=='generate' else judge)
        result=await process_task(inp,view,pack,call,lambda *a:None)
        self.assertEqual(result['status'],'revision_limit_reached')
        self.assertEqual(len(calls),6)
        self.assertEqual(max(n for _,n in calls),2)

    async def test_invalid_generation_consumes_round_and_no_fake_judge(self):
        inp,view,pack,out,judge=fixture();calls=[]
        async def call(role,n,payload):
            calls.append((role,n))
            if role=='generate' and n==0:raise InvalidOutput('{truncated')
            if role=='generate':
                self.assertEqual(payload['revision_feedback']['kind'],'structure_errors')
                return copy.deepcopy(out)
            return judge
        result=await process_task(inp,view,pack,call,lambda *a:None)
        self.assertEqual(result['accepted_revision'],1)
        self.assertEqual(calls,[('generate',0),('generate',1),('judge',1)])

    async def test_invalid_judge_stops_not_passes_or_retries(self):
        inp,view,pack,out,judge=fixture();calls=[]
        async def call(role,n,payload):
            calls.append((role,n))
            if role=='judge':raise InvalidOutput('{truncated')
            return copy.deepcopy(out)
        result=await process_task(inp,view,pack,call,lambda *a:None)
        self.assertEqual(result['status'],'invalid_judge')
        self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
