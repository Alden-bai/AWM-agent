"""Offline reservation/recovery/accounting checks; never call a model."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna_batch as r


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patches=[patch.object(r,'REGISTRY',self.root/'registry'),patch.object(r,'RUN',self.root/'batch'),patch.object(r,'RUN_ID','batch')]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_idempotent_import_and_replay_after_projection_loss(self):
        r.REGISTRY.mkdir()
        b=dict(run_id='history',luna_actual_usage=dict(total_tokens=123))
        t=dict(dataset_id='d',task_id='x:0',run_id='history',status='rejected')
        r.commit('import',[b],[t]);before=(r.REGISTRY/'events.jsonl').read_bytes()
        r.commit('import',[b],[t]);self.assertEqual(before,(r.REGISTRY/'events.jsonl').read_bytes())
        (r.REGISTRY/'tasks.jsonl').unlink()
        r.commit('rebuild')
        bs,ts=r.replay()
        self.assertEqual(len(ts),1);self.assertEqual(bs['history']['luna_actual_usage']['total_tokens'],123)
        self.assertEqual(json.loads((r.REGISTRY/'tasks.jsonl').read_text())['status'],'rejected')

    def test_partial_event_recovery_keeps_prior_events(self):
        r.REGISTRY.mkdir();r.commit('first',[dict(run_id='a')])
        with (r.REGISTRY/'events.jsonl').open('ab') as f:f.write(b'{"incomplete":')
        self.assertIn('a',r.replay()[0]);self.assertEqual(len(list(r.REGISTRY.glob('partial_event_*'))),1)

    def test_usage_does_not_add_cached_or_reasoning_subsets(self):
        v=dict(input_tokens=10,cached_input_tokens=4,output_tokens=5,reasoning_output_tokens=3,total_tokens=15)
        u=dict(thread_id='t',response_usage={'response':v},actual_usage=v,stage='initial_generation')
        result=r.usage_summary([u]);self.assertEqual(result['totals']['total_tokens'],15)
        self.assertEqual(result['totals']['noncached_input_tokens'],6)
        with self.assertRaises(ValueError):r.usage_summary([u,u])
        unknown=r.usage_summary([dict(thread_id='u',usage_unknown=True)])
        self.assertEqual(len(unknown['unknown_usage']),1)

    def test_duplicate_response_across_threads_rejected(self):
        v=dict(input_tokens=1,output_tokens=2,total_tokens=3)
        with self.assertRaises(AssertionError):r.usage_summary([dict(thread_id=t,response_usage={'same':v},actual_usage=v) for t in ('a','b')])

    def test_crash_after_response_recovers_without_resending(self):
        folder=self.root/'call';folder.mkdir()
        identity=dict(run_id='batch')
        r.freeze(folder/'started.json',dict(identity=identity))
        r.freeze(folder/'events.jsonl',json.dumps(dict(type='thread.started',thread_id='t'))+'\n'+json.dumps(dict(type='turn.completed'))+'\n',raw=True)
        r.freeze(folder/'response.txt','{"result":1}',raw=True)
        with patch.object(r.base,'thread_usage',return_value=dict(thread_id='t',actual_config_verified=True,usage_unknown=False,actual_usage=dict(total_tokens=1))):
            self.assertEqual(r.recover_call(folder,identity),'{"result":1}')
            self.assertEqual(r.recover_call(folder,identity),'{"result":1}')
        self.assertTrue(r.read(folder/'completed.json')['recovered_offline'])

    def test_interrupted_call_is_unknown_and_never_resent(self):
        folder=self.root/'call';folder.mkdir();r.freeze(folder/'started.json',{})
        with self.assertRaises(r.base.CallFailure):r.recover_call(folder,dict(run_id='batch'))
        self.assertTrue(r.read(folder/'usage.json')['usage_unknown'])

    def test_history_exclusion_sampling_and_frozen_resume(self):
        assets={name:{} for name in ('gen_tasks','gen_spec','gen_db')}
        tasks=dict(scenario='s',tasks=['Create a note '+str(i) for i in range(6)])
        spec=dict(scenario='s',api_spec=dict(api_groups=[dict(endpoints=[])]))
        db=dict(scenario='s',db_schema=dict(tables=[]))
        data=self.root/'data';data.mkdir()
        for name,obj in zip(assets,(tasks,spec,db)):
            raw=json.dumps(obj);assets[name]['s']=(obj,raw);(data/(name+'.jsonl')).write_text(raw+'\n')
        prompts=self.root/'prompts';prompts.mkdir()
        for name in ('generate','judge','revise','execute','id_rules'):(prompts/(name+'.txt')).write_text('frozen '+name)
        historical=dict(source_file_hashes={n:r.digest(data/(n+'.jsonl')) for n in assets},source_method='full',excluded_data=[])
        r.REGISTRY.mkdir()
        r.commit('history',[dict(run_id='history')],[dict(dataset_id=r.DATASET_ID,task_id='s:0',run_id='history',task_sha256=hashlib.sha256(tasks['tasks'][0].encode()).hexdigest())])
        with patch.object(r,'DATA',data),patch.object(r,'PROMPTS',prompts),patch.object(r,'SIZE',3),patch.object(r,'import_history',return_value=historical):
            m=r.prepare();self.assertEqual(m['sample_size'],3);self.assertNotIn('s:0',[t['task_id'] for t in m['tasks']])
            self.assertEqual(len(r.replay()[1]),4)
            original_digest=r.digest(r.RUN/'manifest.json')
            self.assertEqual(r.prepare(),m);self.assertEqual(original_digest,r.digest(r.RUN/'manifest.json'))
            self.assertEqual(len(r.replay()[1]),4)
            with patch.object(r.random.Random,'sample',side_effect=AssertionError('must not resample')):r.prepare()

    def test_report_denominator_comes_from_manifest(self):
        # Report with three frozen samples and one synthetic terminal task.
        r.RUN.mkdir();(r.RUN/'tasks/a__0').mkdir(parents=True)
        m=dict(sample_size=3,tasks=[dict(task_id='a:0')])
        r.snapshot(r.RUN/'tasks/a__0/status.json',dict(task_id='a:0',status='passed',first_status='passed',entered_revision=False))
        with patch.dict(r.TASKS,{'a:0':{}},clear=True),patch.object(r,'batch_record',return_value=dict(run_id='batch')),patch.object(r,'task_record',return_value=dict(dataset_id='d',task_id='a:0',run_id='batch')),patch.object(r,'cumulative_report'):
            result=r.report(m)
        self.assertEqual(result['denominator'],3);self.assertFalse(result['completed']);self.assertIn('1/3',(r.RUN/'report_zh.md').read_text())


if __name__=='__main__':unittest.main()
