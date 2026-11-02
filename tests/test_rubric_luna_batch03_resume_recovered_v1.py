import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rubric_luna_batch03_resume_recovered_v1 as resume


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.business, cls.contract, cls.evidence = resume.preflight()

    def controller(self):
        return resume.Continuation(self.manifest, self.business, self.contract)

    def test_all_316_cached_stages_are_reused_with_zero_dispatch(self):
        controller = self.controller()
        async def check():
            count = 0
            for path in resume.RUN.glob('tasks/*/round_*/*/identity.json'):
                identity = resume.read(path)
                pack = resume.read(path.parent / 'input.json')
                raw = await controller.call(pack['role'], identity['revision'], identity['task_id'], pack['data'], None)
                self.assertIsInstance(raw, str)
                self.assertTrue(raw)
                count += 1
            self.assertEqual(count, 316)
        with patch.object(resume.recovery, 'execute_one', new=AsyncMock(side_effect=AssertionError('paid dispatch forbidden'))) as paid:
            asyncio.run(check())
        paid.assert_not_called()
        self.assertEqual(len(controller.state['old_remote_holds']), 6)

    def test_changed_payload_is_not_silently_reused_or_dispatched(self):
        controller = self.controller()
        with patch.object(resume.recovery, 'execute_one', new=AsyncMock(side_effect=AssertionError('paid dispatch forbidden'))) as paid:
            with self.assertRaisesRegex(ValueError, 'payload_changed'):
                asyncio.run(controller.call('generate', 0, 'crm_applicant_tracking_1:1', {'changed': True}, None))
        paid.assert_not_called()

    def test_queue_skips_terminal_tasks_and_drains_only_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'run'
            cont = Path(temp) / 'continuation'
            existing = run / 'tasks/finished__0/status.json'
            resume.durable(existing, {'task_id': 'finished:0', 'status': 'passed'})
            controller = resume.Continuation.__new__(resume.Continuation)
            controller.manifest = {'tasks': [{'task_id': t} for t in ['finished:0', 'a:0', 'b:0', 'c:0', 'd:0']]}
            controller.state = {'stop_reason': None, 'old_remote_holds': {str(i): {} for i in range(6)}, 'active': {}}
            controller.state_path = cont / 'scheduler_state.json'
            seen, active, peak = [], 0, 0
            async def process(task, sem):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                seen.append(task['task_id'])
                await asyncio.sleep(.01)
                active -= 1
                return {'status': 'passed'}
            controller.business = types.SimpleNamespace(process=process)
            controller.report = lambda: None
            with patch.object(resume, 'RUN', run), patch.object(resume, 'CONT', cont):
                asyncio.run(controller.run())
            self.assertEqual(set(seen), {'a:0', 'b:0', 'c:0', 'd:0'})
            self.assertLessEqual(peak, 3)
            self.assertEqual(len(controller.state['old_remote_holds']), 6)

    def test_run_requires_explicit_continuation_flag(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/rubric_luna_batch03_resume_recovered_v1.py'), 'run'],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--continue-remaining', result.stderr)

    def test_registry_keeps_old_unknown_usage_separate_from_passed_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'runs/rubric_luna_high_3000_batch03'
            registry = root / 'runs/rubric_luna_registry'
            cont = root / 'continuation'
            records = [dict(dataset_id='awm-dde80a0', task_id=f't:{i}', run_id=run.name, status='reserved') for i in range(3000)]
            history = [dict(dataset_id='awm-dde80a0', task_id=f'h:{i}', run_id='historical', status='passed') for i in range(4000)]
            resume.recovery.append(registry / 'events.jsonl', dict(batches=[{'run_id':run.name}, {'run_id':'historical'}], tasks=records+history))
            controller = resume.Continuation.__new__(resume.Continuation)
            controller.manifest = {'dataset_id':'awm-dde80a0'}
            controller.index = {}
            controller.contract = {'selections':{}}
            controller.business = types.SimpleNamespace(STAGES=())
            states = {'t:0':dict(status='passed', accepted_revision=0, rounds=[dict(revision=0,status='passed')])}
            usages = [dict(task_id='t:0', thread_id='old', usage_unknown=True, artifact_ref='old_usage.json'),
                      dict(task_id='t:0', thread_id='new', usage_unknown=False, artifact_ref='new_usage.json')]
            progress = dict(pending=2999, terminal=1, statuses={'passed':1}, known_usage={'total_tokens':100})
            with patch.object(resume, 'ROOT', root), patch.object(resume, 'RUN', run), patch.object(resume, 'CONT', cont):
                controller.project_registry(states, usages, progress)
                controller.project_registry(states, usages, progress)
            rows = [json.loads(line) for line in (registry/'tasks.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 7000)
            row = next(r for r in rows if r['task_id']=='t:0')
            self.assertEqual(row['status'], 'passed')
            self.assertEqual(row['unknown_usage_refs'], ['old_usage.json'])
            self.assertEqual(row['actual_usage_refs'], ['new_usage.json', 'old_usage.json'])
            self.assertEqual([r for r in rows if r['run_id']=='historical'], sorted(history,key=lambda r:r['task_id']))
            self.assertEqual(len((registry/'events.jsonl').read_text(encoding='utf-8').splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
