import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rubric_qwen_v10_thinking import BASE, RUN, THINKING_BUDGET, ThinkingBudget, paired_request, reservation, read_json
from harness_awm.backend import BudgetExceeded


class ThinkingPilotTests(unittest.TestCase):
    def test_paired_frozen_requests(self):
        for t in read_json(RUN / 'manifest.json')['tasks']:
            name = t['task_id'].replace(':', '__')
            old = read_json(BASE / 'initial' / name / 'request.json')
            original = json.dumps(old, sort_keys=True)
            new = paired_request(old)
            self.assertEqual(json.dumps(old, sort_keys=True), original)
            self.assertEqual(new, read_json(RUN / 'initial' / name / 'planned_request.json'))
            self.assertEqual({k:v for k,v in new.items() if k != 'extra_body'},
                             {k:v for k,v in old.items() if k != 'extra_body'})
            self.assertEqual(new['extra_body'], {'enable_thinking': True, 'thinking_budget': THINKING_BUDGET})
            self.assertLess(reservation(new), 128000)

    def test_budget_reserves_reasoning_before_call(self):
        req = {'messages': [{'role': 'user', 'content': 'test'}], 'max_tokens': 8000,
               'extra_body': {'thinking_budget': THINKING_BUDGET}}
        upper = reservation(req)
        with tempfile.TemporaryDirectory() as tmp:
            low = ThinkingBudget(Path(tmp)/'low.sqlite', 1, upper-1)
            with self.assertRaises(BudgetExceeded):
                low.reserve('a', 'test', req['messages'], 8000)
            exact = ThinkingBudget(Path(tmp)/'exact.sqlite', 1, upper)
            rid = exact.reserve('b', 'test', req['messages'], 8000)
            self.assertEqual(exact.summary()['tokens_accounted_or_reserved'], upper)
            exact.finish(rid, {'prompt_tokens': 10, 'completion_tokens': 90, 'total_tokens': 100})
            self.assertEqual(exact.summary()['tokens_accounted_or_reserved'], 100)


if __name__ == '__main__': unittest.main()
