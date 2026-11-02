import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna_batch03_pilot as pilot


class PilotTests(unittest.IsolatedAsyncioTestCase):
    def test_only_started_unfinished_tasks_are_selected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest = dict(tasks=[dict(task_id=f't:{i}') for i in range(3)])
            for i in (0, 1):
                stage = root/'tasks'/f't__{i}'/'round_0'/'generate'
                stage.mkdir(parents=True)
                (stage/'started.json').write_text('{}')
            (root/'tasks'/'t__0'/'status.json').write_text('{}')
            self.assertEqual(pilot.select_pending(manifest, root), [manifest['tasks'][1]])
            (root/'tasks'/'t__1'/'status.json').write_text('{}')
            with self.assertRaises(ValueError):
                pilot.select_pending(manifest, root)

    async def test_scope_completion_closes_gate_without_waiting_for_deadline(self):
        selected = [dict(task_id='t:1')]
        manifest = dict(sample_size=3000, tasks=[dict(task_id='t:0')]+selected)
        instances = []

        class Trial:
            def __init__(self, scoped, seconds, max_calls):
                self.results = {}
                self.gate = self
                self.stopped = asyncio.Event()
                self.scoped = scoped
                self.limits = (seconds, max_calls)
                instances.append(self)

            async def stop(self, reason):
                self.reason = reason
                self.stopped.set()

            async def run(self):
                self.results['t:1'] = dict(status='passed')
                await self.stopped.wait()
                return self.reason

        with patch.object(pilot.batch, 'Trial', Trial):
            result = await asyncio.wait_for(pilot.execute(manifest, selected), 2)
        self.assertEqual(result, 'pilot_scope_complete')
        self.assertEqual(instances[0].scoped['tasks'], selected)
        self.assertEqual(instances[0].limits, (180, 13))
        self.assertEqual(len(manifest['tasks']), 2)


if __name__ == '__main__':
    unittest.main()
