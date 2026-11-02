"""Contract checks for common invalid generations; no model calls."""
import copy
import json
import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from rubric_qwen_v10 import PROMPT, validate_contract


def sample():
    example = json.loads(PROMPT.read_text().split('完整合法输出是：\n')[1].split('\n\n【输出前')[0])
    inp = {'task_id': 'example', 'task': 'Create a note titled Alpha with body Beta.',
           'sources': [{'id': 'task', 'text': 'Create a note titled Alpha with body Beta.'}], 'quality_policy': None}
    return example, inp


class ContractTests(unittest.TestCase):
    def test_worked_example(self):
        self.assertEqual(validate_contract(*sample()), [])

    def test_reject_invalid_generations(self):
        for fault in ['task', 'quote', 'missing_gp', 'empty_gp', 'decision', 'string_item', 'extra_root', 'coverage', 'unresolved', 'quality']:
            with self.subTest(fault=fault):
                out, inp = copy.deepcopy(sample())
                item = out['rubric'][0]
                if fault == 'task': out['task_id'] = 'other'
                elif fault == 'quote': item['sources'][0]['quote'] = 'invented citation'
                elif fault == 'missing_gp': item['criterion'] = '笔记标题与正文'
                elif fault == 'empty_gp': item['criterion'] = '要求：新建\nG：\nP：'
                elif fault == 'decision': item['decision']['violated_when'] = '本条P不成立'
                elif fault == 'string_item': out['rubric'].append('R02')
                elif fault == 'extra_root': out['score'] = 1
                elif fault == 'coverage': out['coverage'][0]['item_ids'] = []
                elif fault == 'unresolved': out['unresolved'] = [{'issue': '', 'affected_items': ['R01'], 'needed': ''}]
                elif fault == 'quality': item['requirement_level'] = 'quality'
                self.assertTrue(validate_contract(out, inp))


if __name__ == '__main__':
    unittest.main()
