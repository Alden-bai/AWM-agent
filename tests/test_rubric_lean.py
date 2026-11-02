import importlib.util
import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import rubric_lean as lean

class CompactRubricTest(unittest.TestCase):
    def test_frozen_packets_keep_parameter_rules_and_constraints(self):
        for task in lean.read_json(lean.RUN/'manifest.json')['tasks']:
            original=lean.read_json(Path(task['original_ref']))
            packet,refs=lean.compact(original)
            texts={s['id']:s['text'] for s in original['sources']}
            self.assertTrue(all(r['quote'] in texts[r['source_id']] for r in refs.values()))
            self.assertEqual(packet['facts'][0][2],original['task'])
            for s in original['sources']:
                if s['kind']=='schema':self.assertIn(s['text'],[r['quote'] for r in refs.values()])
                if s['kind']=='public_contract':
                    for p in json.loads(s['text']).get('request_params',{}).values():
                        if p.get('description'):
                            quote=json.dumps(p['description'],ensure_ascii=False)[1:-1]
                            self.assertIn(quote,[r['quote'] for r in refs.values()])
    def test_budget_and_no_feedback_in_generation(self):
        m=lean.read_json(lean.RUN/'manifest.json')
        self.assertEqual(m['max_total_tokens'],800000)
        self.assertEqual(len(m['tasks']),10)
        for t in m['tasks']:
            p=lean.read_json(lean.RUN/t['task_id'].replace(':','__')/'input.json')
            self.assertEqual(set(p),{'task_id','facts','note'})
    def test_expand_one_predicate_and_reject_invalid_source(self):
        t=lean.read_json(lean.RUN/'manifest.json')['tasks'][0]
        inp=lean.read_json(Path(t['original_ref']))
        refs=lean.read_json(lean.RUN/t['task_id'].replace(':','__')/'provenance.json')
        x={'items':[{'c':'本次评论','s':[0],'e':'本次评论完整记录','p':'存在同一条r满足指定评分、标题和正文','g':'目标已唯一确定','m':'h'}],'unresolved':[]}
        out=lean.expand(x,inp,refs);d=out['rubric'][0]['decision']
        self.assertIn(x['items'][0]['p'],d['satisfied_when']);self.assertIn(x['items'][0]['p'],d['violated_when'])
        self.assertIn('同一P为假',d['violated_when'])
        x['items'][0]['s']=[99999]
        with self.assertRaises(ValueError):lean.expand(x,inp,refs)

if __name__=='__main__':unittest.main()
