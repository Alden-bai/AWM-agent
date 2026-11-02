"""Pipeline policy checks using synthetic responses; no paid calls."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import rubric_luna100 as runner
from rubric_candidates_100 import prepare_input, DIMENSIONS


class PipelineTests(unittest.TestCase):
    def execute_case(self, invalid=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_id = 'test:0'
            original = dict(task_id=task_id, task='Create a note.', sources=[dict(id='task', kind='task', text='Create a note.')])
            view, audit = prepare_input(original)
            task = dict(task_id=task_id, preparation_status='ready')
            for name, value in [('original', original), ('view', view), ('audit', audit)]:
                task[name+'_ref'] = str(runner.freeze(root/(name+'.json'), value))
            good = dict(task_id=task_id, selection=[dict(candidate_id='C01', action='keep', item_ids=['R01'], reason='Explicit')],
                        items=[dict(id='R01', candidate_ids=['C01'], contract_ref_ids=[], requirement_level='required',
                                    requirement='Create', gate='Records complete', predicate='New record exists',
                                    evidence=[dict(description='Before and after', source_id='task')], method='deterministic')], unresolved=[])
            calls = []

            async def fake(role, revision, tid, payload, sem):
                calls.append((role, revision, payload))
                if role == 'judge':
                    self.assertNotIn('previous_output', payload)
                    self.assertNotIn('revision_feedback', payload)
                    score = 0 if revision == 0 and not invalid else 1
                    return json.dumps(dict(task_id=tid, dimensions=[dict(name=d, score=score, reason='Test', findings=[dict(item_ids=['R01'], candidate_ids=['C01'], problem='Test', counterexample='Test')]) for d in DIMENSIONS]))
                if revision == 0 and invalid:
                    return '{}'
                return json.dumps(good)

            with patch.object(runner, 'RUN', root), patch.object(runner, 'call', fake):
                state = asyncio.run(runner.process(task, asyncio.Semaphore(3)))
            return state, [(role, rev) for role, rev, _ in calls]

    def test_zero_score_gets_one_revision_and_fresh_judge(self):
        state, calls = self.execute_case()
        self.assertEqual(calls, [('generate', 0), ('judge', 0), ('revise', 1), ('judge', 1)])
        self.assertEqual(state['status'], 'passed')

    def test_structure_repair_consumes_only_revision(self):
        state, calls = self.execute_case(invalid=True)
        self.assertEqual(calls, [('generate', 0), ('revise', 1), ('judge', 1)])
        self.assertTrue(state['entered_revision'])


if __name__ == '__main__':
    unittest.main()
