"""Additional lifecycle checks, without altering the frozen implementation files."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna_batch03 as r
import test_rubric_luna_batch03 as fixtures
from rubric_luna_dispatch import DispatchPaused,Gate,Writer


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_does_not_release_slot_before_cleanup(self):
        helper = fixtures.Batch03Tests('test_stop_at_budget_preserves_stage_and_resume_does_not_resend')
        await helper.asyncSetUp()
        try:
            m = helper.fixture()
            sent,cleaning,allow_exit,exit_notice = (asyncio.Event() for _ in range(4))
            holder = []
            async def launcher(*cmd,**kwargs):
                proc = await helper.launcher(*cmd,**kwargs)
                holder.append(proc)
                async def communicate(message):
                    sent.set()
                    await exit_notice.wait()
                    return None,None
                proc.communicate = communicate
                return proc
            async def terminate(proc):
                cleaning.set()
                await allow_exit.wait()
                proc.returncode = -15
                exit_notice.set()
            trial = r.Trial(m,launcher=launcher)
            trial.writer = Writer()
            trial.gate = Gate(trial.save_controller)
            with patch.object(r,'registry_event'),patch.object(r,'terminate_group',side_effect=terminate),\
                 patch.object(r,'group_alive',side_effect=lambda pgid: not exit_notice.is_set()):
                task = asyncio.create_task(trial.call('generate',0,'test:0',r.read(m['tasks'][0]['view_ref']),None))
                await asyncio.wait_for(sent.wait(),2)
                task.cancel()
                await asyncio.wait_for(cleaning.wait(),2)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertEqual(len(trial.gate.active),1)
                self.assertFalse(task.done())
                allow_exit.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(holder[0].returncode)
                self.assertFalse(trial.gate.active)
                self.assertEqual(len(trial.gate.remote_holds),1)
                with self.assertRaises(DispatchPaused):
                    await trial.call('judge',0,'test:0',{},None)
                self.assertFalse((r.RUN/'tasks/test__0/round_0/judge/started.json').exists())
            await trial.writer.close()
        finally:
            await helper.asyncTearDown()

    async def test_actual_local_process_group_is_reaped(self):
        proc = await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(30)',
                    start_new_session=True,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        await r.terminate_group(proc)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(r.group_alive(proc.pid))


if __name__=='__main__':
    unittest.main()
