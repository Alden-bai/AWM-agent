"""Batch03 offline state-machine, reservation and recovery fault injection."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna_batch03 as r
from rubric_candidates_100 import DIMENSIONS


class Batch03Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = []
        # configure() only changes live module wiring; preserve every old binding.
        for obj,keys in [(r.legacy,('RUN','RUN_ID','REGISTRY','STARTUP_PROMPT','SIZE','SEED','DATA','atomic','freeze','snapshot')),
                         (r.legacy.base,('RUN','freeze','snapshot','call'))]:
            for key in keys:
                self.patches.append(patch.object(obj,key,getattr(obj,key)))
        self.patches += [patch.object(r,'RUN',self.root/'batch'),
                         patch.object(r,'REGISTRY',self.root/'registry')]
        for p in self.patches:
            p.start()
        r.REGISTRY.mkdir()
        r.configure()
        self.messages = []
        self.good = dict(task_id='test:0',selection=[dict(candidate_id='C01',action='keep',item_ids=['R01'],reason='Explicit')],
            items=[dict(id='R01',candidate_ids=['C01'],contract_ref_ids=[],requirement_level='required',
                requirement='Create',gate='Records complete',predicate='New record exists',
                evidence=[dict(description='Before and after',source_id='task')],method='deterministic')],unresolved=[])

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def fixture(self):
        original = dict(task_id='test:0',task='Create a note.',sources=[dict(id='task',kind='task',text='Create a note.')])
        view,audit = r.legacy.prepare_input(original)
        task = dict(task_id='test:0',scenario='test',index=0,preparation_status='ready',raw_sources={},
                    task_sha256=hashlib.sha256(original['task'].encode()).hexdigest())
        for name,value in [('original',original),('view',view),('audit',audit)]:
            task[name+'_ref'] = str(r.freeze(self.root/(name+'.json'),value))
            task[name+'_sha256'] = r.digest(task[name+'_ref'])
        for role in ('generate','judge','revise','execute'):
            r.freeze(r.RUN/'prompts'/(role+'.txt'),'frozen '+role,raw=True)
        return dict(tasks=[task],sample_size=1,created_at='frozen',source_file_hashes={'gen_tasks':'source'},dataset_id='d')

    async def launcher(self,*cmd,**kwargs):
        output = Path(cmd[cmd.index('-o')+1])
        case = self
        class FakeProcess:
            pid = 123456789
            returncode = None
            async def communicate(self,message):
                pack = json.loads(message.decode().split('\n',1)[1])
                case.messages.append(pack)
                if pack['role']=='judge':
                    score = 0 if output.parent.parent.name=='round_0' else 1
                    value = dict(task_id='test:0',dimensions=[dict(name=d,score=score,reason='Test',
                        findings=[dict(item_ids=['R01'],candidate_ids=['C01'],problem='Test',counterexample='Test')]) for d in DIMENSIONS])
                else:
                    value = case.good
                output.write_text(json.dumps(value))
                events = [dict(type='thread.started',thread_id='offline-'+str(len(case.messages))),dict(type='turn.completed')]
                kwargs['stdout'].write(('\n'.join(json.dumps(e) for e in events)+'\n').encode())
                kwargs['stdout'].flush()
                self.returncode = 0
                return None,None
            async def wait(self):
                return self.returncode
        return FakeProcess()

    def usage(self,tid):
        values = dict(input_tokens=10,cached_input_tokens=2,output_tokens=5,reasoning_output_tokens=3,total_tokens=15)
        return dict(thread_id=tid,model=r.MODEL,reasoning_effort=r.EFFORT,actual_config_verified=True,
                    usage_unknown=False,actual_usage=values,response_usage={tid:values},tool_call_count=0)

    async def run_trial(self,m,calls):
        with patch.object(r,'registry_event'),patch.object(r,'group_alive',return_value=False),\
             patch.object(r.legacy.base,'thread_usage',side_effect=self.usage):
            trial = r.Trial(m,seconds=.2,max_calls=calls,launcher=self.launcher)
            await trial.run()
        return trial

    async def test_stop_at_budget_preserves_stage_and_resume_does_not_resend(self):
        m = self.fixture()
        first = await self.run_trial(m,1)
        self.assertEqual(first.gate.launched,1)
        self.assertEqual([p['role'] for p in self.messages],['generate'])
        folder = r.RUN/'tasks/test__0'
        self.assertTrue((folder/'round_0/rubric.json').exists())
        self.assertFalse((folder/'round_0/judge/started.json').exists())
        self.assertFalse((folder/'status.json').exists())
        second = await self.run_trial(m,3)
        self.assertEqual(second.gate.launched,3)
        self.assertEqual([p['role'] for p in self.messages],['generate','judge','revise','judge'])
        self.assertEqual(r.read(folder/'status.json')['status'],'passed')
        self.assertTrue((r.RUN/'accepted/test__0.json').exists())
        for p in self.messages:
            if p['role']=='judge':
                self.assertNotIn('previous_output',p['data'])
                self.assertNotIn('revision_feedback',p['data'])
        usage = [r.read(p) for p in folder.glob('round_*/*/usage.json')]
        self.assertEqual(r.legacy.usage_summary(usage)['totals']['total_tokens'],60)

    async def test_complete_business_messages_match_legacy_pipeline(self):
        m = self.fixture()
        await self.run_trial(m,4)
        new_messages = self.messages.copy()
        old = self.root/'legacy'
        old_messages = []
        async def fake(role,revision,tid,payload,sem):
            pack = dict(role=role,execution='frozen execute',protocol='frozen '+role,data=payload)
            old_messages.append(pack)
            if role!='judge':
                return json.dumps(self.good)
            score = 0 if revision==0 else 1
            return json.dumps(dict(task_id=tid,dimensions=[dict(name=d,score=score,reason='Test',
                findings=[dict(item_ids=['R01'],candidate_ids=['C01'],problem='Test',counterexample='Test')]) for d in DIMENSIONS]))
        with patch.object(r.legacy.base,'RUN',old),patch.object(r.legacy.base,'call',fake):
            state = await r.legacy.RAW_PROCESS(m['tasks'][0],None)
        self.assertEqual(state['status'],'passed')
        self.assertEqual(new_messages,old_messages)

    async def test_orphan_request_prevents_dispatch_even_with_no_runner_lock(self):
        folder = r.RUN/'tasks/x__0/round_0/generate'
        r.freeze(folder/'started.json',dict(process=dict(pid=999)))
        with self.assertRaisesRegex(RuntimeError,'prior_request_unresolved'):
            r.orphan_check()

    async def test_response_saved_before_accounting_recovers_offline(self):
        m = self.fixture()
        trial = r.Trial(m)
        trial.writer = r.Writer()
        folder = r.RUN/'recovery'
        identity = dict(stage='initial_generation')
        r.freeze(folder/'identity.json',identity)
        r.freeze(folder/'started.json',{})
        r.freeze(folder/'response.txt','{}',raw=True)
        r.freeze(folder/'events.jsonl',json.dumps(dict(type='thread.started',thread_id='t'))+'\n'+json.dumps(dict(type='turn.completed'))+'\n',raw=True)
        with patch.object(r.legacy.base,'thread_usage',side_effect=self.usage):
            self.assertEqual(await trial.recover(folder,identity),'{}')
            self.assertEqual(await trial.recover(folder,identity),'{}')
        self.assertTrue(r.read(folder/'completed.json')['recovered_offline'])
        await trial.writer.close()

    async def test_atomic_replace_fsyncs_parent_and_does_not_overwrite_frozen_raw(self):
        path = self.root/'raw'
        with patch.object(r,'fsync_dir',wraps=r.fsync_dir) as sync:
            r.freeze(path,'first',raw=True)
            sync.assert_called_with(path.parent)
        with self.assertRaises(ValueError):
            r.freeze(path,'second',raw=True)
        self.assertEqual(path.read_text(),'first')

    async def test_reservation_event_survives_crash_before_manifest_without_resampling(self):
        data = self.root/'data'
        data.mkdir()
        assets = {}
        objects = [dict(scenario='s',tasks=['Create note '+str(i) for i in range(10000)]),
                   dict(scenario='s',api_spec=dict(api_groups=[dict(endpoints=[])])),
                   dict(scenario='s',db_schema=dict(tables=[]))]
        for name,obj in zip(('gen_tasks','gen_spec','gen_db'),objects):
            raw = json.dumps(obj)
            (data/(name+'.jsonl')).write_text(raw+'\n')
            assets[name] = {'s':(obj,raw)}
        hashes = {name:r.digest(data/(name+'.jsonl')) for name in assets}
        audit = dict(ids=['s:0'],source_file_hashes=hashes)
        with patch.object(r,'DATA',data),patch.object(r,'SIZE',3),\
             patch.object(r,'historical_audit',return_value=audit),\
             patch.object(r.legacy,'load_assets',return_value=assets),\
             patch.object(r,'input_row',side_effect=OSError('injected_preparation_crash')):
            with self.assertRaisesRegex(OSError,'injected_preparation_crash'):
                r.prepare()
            selection = r.read(r.RUN/'reservation_intent.json')['selection']
            self.assertEqual(len(selection),3)
            self.assertNotIn('s:0',selection)
            self.assertFalse((r.RUN/'manifest.json').exists())
            _,registered = r.legacy.replay()
            self.assertEqual({t['task_id'] for t in registered.values()},set(selection))
            with patch.object(r.random.Random,'sample',side_effect=AssertionError('resampled')):
                with self.assertRaisesRegex(OSError,'injected_preparation_crash'):
                    r.prepare()
            self.assertEqual(r.read(r.RUN/'reservation_intent.json')['selection'],selection)


if __name__=='__main__':
    unittest.main()
