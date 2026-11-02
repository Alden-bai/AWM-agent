"""Offline checks of the explicit 12-concurrency adapter; no paid model calls."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rubric_luna_batch03_stress import StressGate
from rubric_luna_dispatch import DispatchPaused


class StressTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_gate_can_hold_twelve_then_stops_at_thirteen(self):
        states = []
        async def persist(event,state):
            states.append((event,state))
        async def before():
            pass
        async def spawn():
            return SimpleNamespace(pid=1)
        gate = StressGate(persist,limit=3,max_calls=13)
        for i in range(12):
            await gate.launch(str(i),before,spawn)
        waiting = asyncio.create_task(gate.launch('12',before,spawn))
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())
        self.assertEqual(gate.peak,12)
        await gate.complete('0')
        await waiting
        self.assertEqual(gate.launched,13)
        with self.assertRaises(DispatchPaused):
            await gate.launch('14',before,spawn)
        state = gate.state()
        self.assertEqual(state['initial_concurrency'],12)
        self.assertEqual(state['production_initial_concurrency'],3)
        self.assertTrue(state['diagnostic_override'])
        self.assertFalse(state['promotion_qualification_claimed'])

    async def test_override_never_discards_unknown_requests_or_cooldown(self):
        async def persist(event,state):
            pass
        gate = StressGate(persist,state=dict(remote_holds={'unknown':{}},cooldown_until=10**20))
        self.assertEqual(len(gate.remote_holds),1)
        self.assertEqual(gate.cooldown_until,10**20)
        self.assertEqual(gate.stop_reason,'unresolved_requests_require_reconciliation')


if __name__=='__main__':
    unittest.main()
