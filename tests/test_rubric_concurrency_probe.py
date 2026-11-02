"""Exercise the concurrency/child-lifecycle boundary with local processes only."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_concurrency_probe as r


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.message = self.directory/'message.txt'
        self.message.write_text('offline')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def launch(self, *cmd, **kwargs):
        output = cmd[cmd.index('-o')+1]
        script = ("import pathlib,sys,time; sys.stdin.buffer.read(); time.sleep(.04); "
                  f"pathlib.Path({output!r}).write_text('{{}}'); "
                  "print('{\"type\":\"thread.started\",\"thread_id\":\"offline\"}'); "
                  "print('{\"type\":\"turn.completed\"}')")
        return await asyncio.create_subprocess_exec(sys.executable, '-c', script, **kwargs)

    async def test_global_peak_queue_and_unique_claims(self):
        probe = r.Probe(self.directory, workers=12, launcher=self.launch)
        probe.started = time.monotonic()
        usage = dict(actual_config_verified=True, usage_unknown=False,
                     actual_usage=dict(total_tokens=1), tool_call_count=0)
        with patch.object(r.io.base, 'thread_usage', return_value=usage):
            rows = await asyncio.gather(*(probe.call(str(i), self.message, time.monotonic()+5)
                                          for i in range(25)))
            with self.assertRaisesRegex(ValueError, 'duplicate_request_claim'):
                await probe.call('0', self.message, time.monotonic()+5)
        self.assertEqual(probe.peak, 12)
        self.assertEqual(probe.active, 0)
        self.assertEqual(len(probe.claims), 25)
        self.assertTrue(all(row['completed'] for row in rows))

    async def test_deadline_reaps_child_before_slot_released(self):
        children = []
        async def launcher(*cmd, **kwargs):
            proc = await asyncio.create_subprocess_exec(sys.executable, '-c',
                      'import sys,time; sys.stdin.buffer.read(); time.sleep(20)', **kwargs)
            children.append(proc)
            return proc
        probe = r.Probe(self.directory, workers=1, launcher=launcher)
        probe.started = time.monotonic()
        row = await probe.call('deadline', self.message, time.monotonic()+.1)
        self.assertTrue(row['interrupted'])
        self.assertTrue(row['usage_unknown'])
        self.assertEqual(probe.active, 0)
        self.assertIsNotNone(children[0].returncode)
        self.assertIsNone(await probe.call('expired', self.message, time.monotonic()-1))
        self.assertNotIn('expired', probe.claims)

    async def test_second_runner_rejected(self):
        with r.io.locked(self.directory/'.runner.lock', blocking=False):
            with self.assertRaises(BlockingIOError):
                with r.io.locked(self.directory/'.runner.lock', blocking=False):
                    self.fail('second runner acquired the lock')

    async def test_hard_maximum(self):
        with self.assertRaises(ValueError):
            r.Probe(self.directory, workers=13)
        with self.assertRaises(ValueError):
            r.Probe(self.directory, seconds=181)

    async def test_prepare_only_resume_preserves_manifest_and_rechecks_input(self):
        source = self.directory/'source.json'
        source.write_text('{}')
        message = self.directory/'messages/00.txt'
        r.io.freeze(message, 'message', raw=True)
        manifest = self.directory/'manifest.json'
        r.io.freeze(manifest, dict(workers=1, max_model_calls=2, model=r.io.MODEL,
                    reasoning_effort=r.io.EFFORT, created_at='frozen',
                    inputs=[dict(source_ref=str(source), source_sha256=r.io.digest(source),
                                 message_sha256=r.io.digest(message))]))
        before = manifest.read_bytes()
        self.assertEqual(r.prepare(self.directory, 1), [message])
        self.assertEqual(manifest.read_bytes(), before)
        source.write_text('changed')
        with self.assertRaises(AssertionError):
            r.prepare(self.directory, 1)


if __name__ == '__main__':
    unittest.main()
