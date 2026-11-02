"""Offline production acceptance, with virtual time and no model requests."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna_batch03_production_v1 as p
import rubric_luna_bank_v1 as bank
from rubric_luna_autoscale_v1 import AdaptiveGate, compare, summarize
from rubric_luna_runtime_v1 import DispatchPaused, Writer
import test_rubric_luna_batch03 as fixtures


class ScaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = 1000
        self.records = []
        async def save(event,state):
            self.records.append((event,copy.deepcopy(state)))
        self.gate = AdaptiveGate(save,clock=lambda:self.clock)

    async def samples(self,latency=20,count=40,seconds=301,input_bytes=50000,stage='initial_generation'):
        self.clock += seconds
        for i in range(count):
            key = str(self.clock)+':'+str(i)
            async def before(): pass
            async def spawn(): return SimpleNamespace(pid=987654321)
            await self.gate.launch(key,before,spawn)
            await self.gate.complete(key,detail=dict(stage=stage,input_bytes=input_bytes,
                model_seconds=latency,process_seconds=latency+1,queue_seconds=900,success=True))
        await self.gate.observe()

    async def test_baseline_and_stepwise_promotion_require_time_and_comparable_gain(self):
        await self.samples(seconds=299)
        self.assertEqual(self.gate.limit,3)
        self.clock += 2
        await self.gate.observe()
        self.assertEqual(self.gate.limit,6)
        await self.samples(count=60)
        self.assertEqual(self.gate.limit,9)
        await self.samples(count=90)
        self.assertEqual(self.gate.limit,12)
        await self.samples(count=120)
        self.assertEqual(self.gate.limit,12)
        with self.assertRaises(ValueError):
            await self.gate.set_limit(13)

    async def test_30_completions_required_and_input_or_phase_mix_cannot_force_promotion(self):
        await self.samples(count=29)
        self.assertEqual(self.gate.limit,3)
        await self.samples(count=1,seconds=1)
        self.assertEqual(self.gate.limit,6)
        await self.samples(count=90,input_bytes=200000)
        self.assertEqual(self.gate.limit,6)
        await self.samples(count=90,stage='initial_judge')
        self.assertEqual(self.gate.limit,6)
        self.assertEqual(self.gate.last_decision,'insufficient_comparable_improvement_keep_level')

    async def test_two_slow_windows_downshift_without_cancelling_inflight(self):
        await self.samples(count=40)
        self.assertEqual(self.gate.limit,6)
        await self.samples(latency=45,count=40)
        self.assertEqual(self.gate.limit,6)
        await self.samples(latency=45,count=40)
        self.assertEqual(self.gate.limit,3)

    async def test_restart_preserves_cooldown_and_circuit_and_reobserves_level(self):
        await self.samples()
        self.gate.cooldown_until = self.clock+600
        state = self.gate.state()
        restored = AdaptiveGate(self.gate.persist,state=state,clock=lambda:self.clock+10)
        self.assertEqual(restored.limit,6)
        self.assertEqual(restored.cooldown_until,state['cooldown_until'])
        self.assertFalse(restored.window_rows)
        await self.gate.stop('infrastructure_circuit_open')
        restored = AdaptiveGate(self.gate.persist,state=self.gate.state())
        with self.assertRaises(DispatchPaused):
            await restored.launch('no',None,None)

    async def test_mixed_2400_calls_long_virtual_soak_and_12_to_6(self):
        self.gate.limit = 12
        async def before(): pass
        async def spawn(): return SimpleNamespace(pid=987654321)
        for turn in range(200):
            for i in range(12):
                key = f'{turn}:{i}'
                await self.gate.launch(key,before,spawn)
            self.assertEqual(len(self.gate.active),12)
            await self.gate.set_limit(6)
            waiting = asyncio.create_task(self.gate.launch(f'next:{turn}',before,spawn))
            await asyncio.sleep(0)
            for i in range(7):
                self.clock += 2
                await self.gate.complete(f'{turn}:{i}',detail=dict(stage=str(i%4),input_bytes=50000,
                    model_seconds=20,success=True))
            await waiting
            self.assertEqual(len(self.gate.active),6)
            for i in range(7,12):
                await self.gate.complete(f'{turn}:{i}')
            await self.gate.complete(f'next:{turn}')
            await self.gate.set_limit(12)
        self.assertLessEqual(self.gate.peak,12)
        self.assertEqual(self.gate.launched,2600)
        self.assertGreater(self.clock-1000,1800)

    async def test_late_old_level_completion_does_not_qualify_new_level(self):
        async def before(): pass
        async def spawn(): return SimpleNamespace(pid=1)
        await self.gate.launch('old',before,spawn)
        self.gate.limit=6
        await self.gate.complete('old',detail=dict(stage='initial_generation',input_bytes=50000,model_seconds=20,success=True))
        self.assertFalse(self.gate.window_rows)

    async def test_rate_limit_downshifts_resets_observation_and_honors_retry_after(self):
        self.gate.limit=9
        async def before(): pass
        async def spawn(): return SimpleNamespace(pid=1)
        await self.gate.launch('one',before,spawn)
        await self.gate.complete('one',rate_limited=True,infrastructure_failure=True,retry_after=900)
        self.assertEqual(self.gate.limit,6)
        self.assertGreaterEqual(self.gate.cooldown_until,self.clock+900)
        self.assertFalse(self.gate.window_rows)


class ProductionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = fixtures.Batch03Tests('test_stop_at_budget_preserves_stage_and_resume_does_not_resend')
        await self.helper.asyncSetUp()
        self.patches = [patch.object(p,'RUN',self.helper.root/'batch'),
            patch.object(p,'REGISTRY',self.helper.root/'registry'),patch.object(p,'group_alive',return_value=False),
            patch.object(p.batch,'registry_event'),patch.object(p,'measured_usage',side_effect=lambda tid,wall:self.helper.usage(tid))]
        for item in self.patches: item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches): item.stop()
        await self.helper.asyncTearDown()

    async def test_full_four_stage_pipeline_and_terminal_resume_without_calls(self):
        manifest = self.helper.fixture()
        p.freeze(p.RUN/'manifest.json',manifest)
        trial = p.Production(manifest,launcher=self.helper.launcher)
        summary = await trial.run()
        self.assertEqual(summary['terminal_count'],1)
        self.assertEqual(len(self.helper.messages),4)
        self.assertEqual([m['role'] for m in self.helper.messages],['generate','judge','revise','judge'])
        state = p.read(p.RUN/'scheduler_state.json')
        self.assertFalse(state['active'])
        for row in state['stage_completions']:
            self.assertGreaterEqual(row['queue_seconds'],0)
            self.assertGreaterEqual(row['process_seconds'],0)
            self.assertNotIn('elapsed_seconds',row)
        raw = {str(f):p.digest(f) for f in p.RUN.glob('tasks/*/round_*/*/response.txt')}
        second = p.Production(manifest,launcher=self.helper.launcher)
        await second.run()
        self.assertEqual(len(self.helper.messages),4)
        self.assertTrue(all(p.digest(f)==sha for f,sha in raw.items()))
        self.assertEqual(p.legacy.usage_summary(list(second.usage_rows.values()))['totals']['total_tokens'],60)

    async def test_production_and_old_complete_business_messages_identical(self):
        manifest = self.helper.fixture()
        p.freeze(p.RUN/'manifest.json',manifest)
        await self.helper.run_trial(manifest,4)
        expected = self.helper.messages.copy()
        fresh = self.helper.root/'production'
        fresh.mkdir()
        for file in (p.RUN/'prompts').glob('*'):
            p.freeze(fresh/'prompts'/file.name,file.read_text(),raw=True)
        p.freeze(fresh/'manifest.json',manifest)
        self.helper.messages=[]
        with patch.object(p,'RUN',fresh),patch.object(p.batch,'RUN',fresh):
            trial = p.Production(manifest,launcher=self.helper.launcher)
            await trial.run()
        self.assertEqual(self.helper.messages,expected)

    async def test_second_runner_lock_rejected(self):
        with p.legacy.locked(p.RUN/'.runner.lock',blocking=False):
            with self.assertRaises(BlockingIOError):
                with p.legacy.locked(p.RUN/'.runner.lock',blocking=False):
                    self.fail('second runner acquired lock')

    async def test_unknown_remote_stops_and_does_not_become_terminal_business_failure(self):
        manifest = self.helper.fixture()
        p.freeze(p.RUN/'manifest.json',manifest)
        async def no_completion(*cmd,**kwargs):
            proc = await self.helper.launcher(*cmd,**kwargs)
            async def communicate(message):
                kwargs['stdout'].write(b'{"type":"thread.started","thread_id":"unknown"}\n')
                kwargs['stdout'].flush()
                proc.returncode=1
                return None,None
            proc.communicate=communicate
            return proc
        trial = p.Production(manifest,launcher=no_completion)
        await trial.run()
        self.assertEqual(trial.gate.stop_reason,'unresolved_remote_request')
        self.assertEqual(len(trial.gate.remote_holds),1)
        self.assertFalse((p.RUN/'tasks/test__0/status.json').exists())

    async def test_publication_crash_recovery_and_idempotent_export(self):
        for item in (patch.object(p,'RUN_ID',p.RUN.name),patch.object(p.batch,'RUN_ID',p.RUN.name)):
            item.start()
            self.patches.append(item)
        manifest = self.helper.fixture()
        p.freeze(p.RUN/'manifest.json',manifest)
        trial = p.Production(manifest,launcher=self.helper.launcher)
        await trial.run()
        p.snapshot(p.RUN/'usage.json',p.legacy.usage_summary(list(trial.usage_rows.values())))
        record = p.legacy.task_record(manifest,manifest['tasks'][0],p.RUN,p.RUN_ID)
        batches = {p.RUN_ID:dict(run_id=p.RUN_ID,manifest_ref=str(p.RUN/'manifest.json'))}
        records = {(manifest['dataset_id'],'test:0',p.RUN_ID):record}
        for name,value in [('batches.json',batches),('tasks.jsonl',record)]:
            p.freeze(p.REGISTRY/name,value)
        p.freeze(p.REGISTRY/'events.jsonl','',raw=True)
        p.freeze(p.REGISTRY/'README.md','registry',raw=True)
        original_snapshot = p.batch.snapshot
        def crash_on_entry(path,value):
            if Path(path).name=='bank_manifest.json':
                raise OSError('injected_publication_crash')
            return original_snapshot(path,value)
        with patch.object(p.batch,'HISTORY',[]),patch.object(p.legacy,'replay',return_value=(batches,records)),\
             patch.object(p.legacy,'execution_manifest',return_value=(manifest,[])),\
             patch.object(p.legacy,'verify'),patch.object(p.legacy,'commit'),patch.object(p.legacy,'cumulative_report'):
            with patch.object(p.batch,'snapshot',side_effect=crash_on_entry):
                with self.assertRaisesRegex(OSError,'injected_publication_crash'):
                    bank.publish()
            self.assertFalse((p.REGISTRY/'bank_manifest.json').exists())
            result = bank.publish()
            self.assertEqual(result['total_questions'],1)
            self.assertEqual(result['passed'],1)
            manifest_hash = p.digest(p.REGISTRY/'bank_manifest.json')
            again = bank.publish()
            self.assertEqual(result,again)
            self.assertEqual(manifest_hash,p.digest(p.REGISTRY/'bank_manifest.json'))

    async def test_disk_failure_before_send_prevents_any_model_launch(self):
        manifest = self.helper.fixture()
        trial = p.Production(manifest,launcher=self.helper.launcher)
        trial.writer = Writer()
        trial.gate = AdaptiveGate(trial.save_controller)
        def fail(*args,**kwargs):
            raise OSError('injected_disk_failure')
        with patch.object(p,'freeze',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'injected_disk_failure'):
                await trial.call('generate',0,'test:0',p.read(manifest['tasks'][0]['view_ref']),None)
        self.assertFalse(self.helper.messages)
        self.assertFalse(trial.gate.active)
        self.assertEqual(trial.gate.stop_reason,'writer_failure')
        await trial.writer.close()


class BankTests(unittest.TestCase):
    def test_business_output_cannot_be_mistaken_for_transport_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p.freeze(root/'stderr.txt','model list refresh timed out',raw=True)
            p.freeze(root/'events.jsonl',json.dumps(dict(type='item.completed',item=dict(text='Handle HTTP 429 rate limits')))+'\n',raw=True)
            errors = p.transport_errors(root)
            self.assertNotIn('429',errors)
            self.assertIn('model list',errors)

    def test_five_dimensions_no_zero_validated_before_accepting(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            folder = directory/'tasks/a__0'
            p.freeze(folder/'status.json',dict(status='passed',rounds=[{}],accepted_revision=0))
            p.freeze(folder/'round_0/rubric.json',dict(task_id='a:0',rubric=[]))
            p.freeze(directory/'accepted.json',dict(task_id='a:0',rubric=[]))
            p.freeze(directory/'original.json',dict(task_id='a:0'))
            p.freeze(directory/'audit.json',dict(candidates=[]))
            p.freeze(folder/'round_0/generate/output.json',{})
            p.freeze(folder/'round_0/judge/output.json',{})
            task=dict(task_id='a:0',original_ref=str(directory/'original.json'),audit_ref=str(directory/'audit.json'))
            record=dict(accepted_ref=str(directory/'accepted.json'))
            with patch.object(bank.b.legacy.base,'validate_selection',return_value=[]),patch.object(bank.b.legacy.base,'compile_rubric',return_value=dict(task_id='a:0',rubric=[])),patch.object(bank.b.legacy.base,'evaluate',return_value=dict(status='rejected')):
                with self.assertRaises(AssertionError):
                    bank.accepted_record(task,record,directory)


if __name__=='__main__': unittest.main()
