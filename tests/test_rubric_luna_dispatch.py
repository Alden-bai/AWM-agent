"""Offline dispatch tests: no model, network, or production registry access."""
import asyncio
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rubric_luna_dispatch import Gate, Writer, DispatchPaused


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = []
        self.spawned = 0

    async def persist(self,event,state):
        self.states.append((event,copy.deepcopy(state)))

    async def before(self):
        pass

    async def spawn(self):
        self.spawned += 1
        return SimpleNamespace(pid=self.spawned)

    async def test_mixed_queue_downshift_retains_existing_permits(self):
        gate = Gate(self.persist,limit=12)
        for i in range(12):
            await gate.launch(f'{i%4}:{i}',self.before,self.spawn)
        waiting = asyncio.create_task(gate.launch('next',self.before,self.spawn))
        await gate.set_limit(6)
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())
        self.assertEqual(len(gate.active),12)
        for i in range(6):
            await gate.complete(f'{i%4}:{i}')
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())  # At six in-flight there is still no free permit.
        await gate.complete('2:6')
        await asyncio.wait_for(waiting,1)
        self.assertEqual(len(gate.active),6)
        self.assertEqual(gate.peak,12)
        self.assertEqual(self.spawned,13)
        with self.assertRaises(DispatchPaused):
            await gate.launch('over-budget',self.before,self.spawn)

    async def test_stop_wins_during_durable_intent_and_waiting_for_slot(self):
        gate = Gate(self.persist,limit=1)
        await gate.launch('one',self.before,self.spawn)
        waiting = asyncio.create_task(gate.launch('two',self.before,self.spawn))
        await asyncio.sleep(.01)
        await gate.stop('user_stop')
        with self.assertRaises(DispatchPaused):
            await waiting
        self.assertEqual(self.spawned,1)
        await gate.complete('one')
        gate = Gate(self.persist)
        async def stop_during_write():
            gate.requested_stop = 'user_stop'
        with self.assertRaises(DispatchPaused):
            await gate.launch('three',stop_during_write,self.spawn)
        self.assertFalse(gate.active)
        self.assertEqual(self.spawned,1)

    async def test_cancel_during_spawn_waits_for_process_identity(self):
        gate = Gate(self.persist)
        ready, release = asyncio.Event(),asyncio.Event()
        async def slow_spawn():
            ready.set()
            await release.wait()
            return await self.spawn()
        task = asyncio.create_task(gate.launch('one',self.before,slow_spawn))
        await ready.wait()
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        self.assertIn('one',gate.active)
        release.set()
        proc,cancelled = await task
        self.assertTrue(cancelled)
        self.assertEqual(proc.pid,1)
        self.assertIn('one',gate.active)
        await gate.complete('one',unresolved=True)
        self.assertIn('one',gate.remote_holds)

    async def test_three_failures_or_five_of_recent_twenty_open_circuit(self):
        for failures in ([True]*3,[True,False]*4+[True]):
            gate = Gate(self.persist)
            for i,failed in enumerate(failures):
                await gate.launch(str(i),self.before,self.spawn)
                await gate.complete(str(i),infrastructure_failure=failed)
            self.assertEqual(gate.stop_reason,'infrastructure_circuit_open')
            restored = Gate(self.persist,state=gate.state())
            with self.assertRaises(DispatchPaused):
                await restored.launch('no',self.before,self.spawn)

    async def test_successful_model_call_does_not_count_business_failure(self):
        gate = Gate(self.persist)
        for i in range(5):
            await gate.launch(str(i),self.before,self.spawn)
            await gate.complete(str(i),detail=dict(business_verdict='invalid_judge'))
        self.assertFalse(gate.stop_reason)
        self.assertEqual(sum(gate.recent),0)

    async def test_rate_limit_wait_not_shortened_on_restart(self):
        gate = Gate(self.persist,limit=6,clock=lambda:100)
        await gate.launch('one',self.before,self.spawn)
        await gate.complete('one',rate_limited=True,infrastructure_failure=True,retry_after=600)
        self.assertEqual(gate.limit,3)
        self.assertGreaterEqual(gate.cooldown_until,700)
        restored = Gate(self.persist,state=gate.state(),clock=lambda:101)
        waiting = asyncio.create_task(restored.launch('no',self.before,self.spawn))
        await asyncio.sleep(.02)
        self.assertFalse(waiting.done())
        await restored.stop('user_stop')
        with self.assertRaises(DispatchPaused):
            await waiting

    async def test_remote_unknown_and_persisted_active_never_freed_by_timeout(self):
        gate = Gate(self.persist)
        await gate.launch('one',self.before,self.spawn)
        await gate.complete('one',unresolved=True)
        restored = Gate(self.persist,state=gate.state(),clock=lambda:10**20)
        self.assertEqual(len(restored.remote_holds),1)
        with self.assertRaises(DispatchPaused):
            await restored.launch('no',self.before,self.spawn)
        restored = Gate(self.persist,state=dict(active={'old':{'pid':999}}))
        with self.assertRaises(DispatchPaused):
            await restored.launch('no',self.before,self.spawn)

    async def test_writer_failure_prevents_launch_and_fails_queued_operations(self):
        writer = Writer(maxsize=1)
        def broken():
            raise OSError('injected_disk_failure')
        async def before():
            await writer.submit(broken)
        gate = Gate(self.persist)
        with self.assertRaises(RuntimeError):
            await gate.launch('one',before,self.spawn)
        self.assertEqual(self.spawned,0)
        self.assertEqual(gate.stop_reason,'writer_failure')
        with self.assertRaises(RuntimeError):
            await writer.submit(lambda: self.fail('writer continued after failure'))
        await writer.close()

    async def test_duplicate_stage_and_hard_limit_rejected(self):
        gate = Gate(self.persist)
        await gate.launch('same',self.before,self.spawn)
        await gate.complete('same')
        with self.assertRaises(ValueError):
            await gate.launch('same',self.before,self.spawn)
        with self.assertRaises(ValueError):
            await gate.set_limit(13)


if __name__=='__main__':
    unittest.main()
