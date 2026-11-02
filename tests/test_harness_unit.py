import asyncio
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from harness_awm.backend import Budget, BudgetExceeded, ProcessEvaluator, TrainingActorBackend
from harness_awm.contracts import RunConfig
from harness_awm.evaluation import reward_for
from harness_awm.export import tokenize_turn
from harness_awm.io import Recorder, db_fingerprint, file_hash, snapshot
from harness_awm.protocol import Observations, ProtocolError, parse_action, validate_call
from harness_awm.registry import check_splits
from harness_support import fixture


class ProtocolTests(unittest.TestCase):
    def test_rejects_invalid_actions(self):
        invalid = ['{}{}', '[{"type":"finish","answer":""}]', '{"type":"list_tools","type":"finish"}',
                   '{"type":"list_tools","extra":1}', '{"type":"finish"}', '{"type":[]}',
                   '{"type":"call_tool","name":"x","arguments":{"x":NaN}}',
                   '{"type":"call_tool","name":"x","arguments":{"x":1e999}}',
                   '{"type":"read_observation","observation_id":"a","offset":true,"limit":1}',
                   '```json\n{"type":"list_tools"}\n```', '/tmp/secret.json', '{"type":"finish","answer":0}']
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                parse_action(raw)

    def test_empty_answer_valid(self):
        self.assertEqual(parse_action('{"type":"finish","answer":""}')["answer"], "")

    def test_tool_discovery_and_schema(self):
        a = parse_action('{"type":"call_tool","name":"x","arguments":{"value":"bad"}}')
        with self.assertRaises(ProtocolError):
            validate_call(a, {})
        with self.assertRaises(ProtocolError):
            validate_call(a, {"x": {"inputSchema": {"type": "object", "properties": {"value": {"type": "integer"}}}}})

    def test_observations_are_scoped_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(tmp, "one")
            a, b = Observations(rec, 64, 20), Observations(rec, 64, 20)
            visible, _ = a.present({"records": "中" * 200})
            oid = visible["observation_id"]
            action = {"observation_id": oid, "offset": 65, "limit": 20}
            self.assertEqual(len(a.read(action)["content"]), 20)
            with self.assertRaises(ProtocolError):
                b.read(action)
            with self.assertRaises(ProtocolError):
                a.read({**action, "observation_id": "../../initial.db"})
            with self.assertRaises(ProtocolError):
                a.read({**action, "limit": 21})


class StorageTests(unittest.TestCase):
    def test_sqlite_backup_includes_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "work.db", Path(tmp) / "final.db"
            with sqlite3.connect(src) as db:
                db.execute("pragma journal_mode=wal")
                db.execute("create table t(x)")
                db.execute("insert into t values(80)")
                db.commit()
                snapshot(src, dst)
                self.assertEqual(db_fingerprint(src), db_fingerprint(dst))
                with self.assertRaises(ValueError):
                    snapshot(src, src)

    def test_atomic_cumulative_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Budget(Path(tmp) / "budget.db", 3, 100000)
            def reserve(i):
                try:
                    return b.reserve(str(i), "actor", [], 1)
                except BudgetExceeded:
                    return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                ids = list(pool.map(reserve, range(10)))
            self.assertEqual(sum(x is not None for x in ids), 3)
            before = b.summary()["tokens_accounted_or_reserved"]
            b.finish(next(x for x in ids if x), None)
            self.assertEqual(before, b.summary()["tokens_accounted_or_reserved"])
            with self.assertRaises(BudgetExceeded):
                Budget(Path(tmp) / "closed.db", 0, 0).reserve("x", "actor", [], 1)

    def test_lineage_split_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = fixture(tmp)
            with self.assertRaises(ValueError):
                check_splits([task.model_copy(update={"split": "train"}), task.model_copy(update={"split": "test"})])

    def test_missing_initialization_not_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = fixture(tmp).model_copy(update={"readiness_profile": {"runtime_ready": False}})
            from harness_awm.environment import AWMEnvAdapter
            with self.assertRaisesRegex(ValueError, "runtime_not_ready"):
                asyncio.run(AWMEnvAdapter(task, RunConfig(), Path(tmp) / "episode").prepare())


class RewardTests(unittest.TestCase):
    def test_config_hash_survives_json_round_trip(self):
        from harness_awm.evaluation import judge_config_hash
        original = RunConfig()
        restored = RunConfig.model_validate(json.loads(original.model_dump_json()))
        self.assertEqual(judge_config_hash(original), judge_config_hash(restored))

    def test_classification_and_validity_are_separate(self):
        args = dict(evaluation_valid=True, execution_valid=True, approved=True, test_only=False)
        self.assertEqual(reward_for("complete", **args), {"reward_valid": True, "reward": 1})
        self.assertEqual(reward_for("agent_error", **args)["reward"], 0)
        for kind in ("server_error", "judge_error"):
            self.assertIsNone(reward_for(kind, **args)["reward"])
        for k, v in (("evaluation_valid", False), ("execution_valid", False), ("approved", False), ("test_only", True)):
            self.assertIsNone(reward_for("complete", **{**args, k: v})["reward"])

    def test_future_modules_fail_explicitly(self):
        for cls in (ProcessEvaluator, TrainingActorBackend):
            with self.assertRaises(NotImplementedError):
                cls()

    def test_training_masks_only_current_response(self):
        class Tokenizer:
            def apply_chat_template(self, messages, tokenize, add_generation_prompt):
                text = ''.join(f'<{m["role"]}>{m["content"]}</end>' for m in messages)
                if add_generation_prompt:
                    text += '<assistant>'
                return list(text.encode())
        history = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "old action"},
                   {"role": "user", "content": "environment observation"}]
        x = tokenize_turn(Tokenizer(), history, "new action")
        response = bytes(t for t, mask in zip(x["input_ids"], x["response_mask"]) if mask).decode()
        self.assertEqual(response, "new action</end>")
        self.assertTrue(all(v == -100 for v, mask in zip(x["labels"], x["response_mask"]) if not mask))


if __name__ == "__main__":
    unittest.main()
