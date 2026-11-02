import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from harness_awm.backend import Budget, ScriptedBackend
from harness_awm.contracts import RunConfig
from harness_awm.environment import AWMEnvAdapter
from harness_awm.evaluation import evaluate_episode
from harness_awm.io import file_hash, read_json, rows, verify_seal
from harness_awm.runner import EpisodeRunner
from harness_support import LIST, call, finish, fixture


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awm-harness-test-")
        self.root = Path(self.temp.name)
        self.task = fixture(self.root / "assets")
        self.config = RunConfig(evaluator_timeout=20, startup_timeout=15, tool_timeout=2, stop_timeout=2,
                                mock_classification="complete", context_limit=100000)
        self.budget = Budget(self.root / "budget.db", 0, 0)
        self.sem = asyncio.Semaphore(4)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def episode(self, actions, name="episode", **updates):
        cfg = self.config.model_copy(update=updates)
        result = await EpisodeRunner(cfg, self.budget, self.sem).run(
            self.task, self.root / name, name, ScriptedBackend(actions))
        return result, self.root / name

    async def test_real_mcp_write_and_official_sql_mock_judge(self):
        result, folder = await self.episode([LIST, call("write_value", value=80), call("read_state"), finish("80")])
        self.assertEqual(result["termination_reason"], "finish", result)
        self.assertEqual(result["terminal_state_status"], "trusted", result)
        self.assertEqual(result["evaluation_status"], "test_only", result)
        self.assertEqual(result["outcome_classification"], "complete", result)
        self.assertFalse(result["reward_valid"])
        self.assertIsNone(result["reward"])
        with sqlite3.connect(folder / "final.db") as db:
            self.assertEqual(db.execute("select value from items").fetchone()[0], 80)
        attempt = next((folder / "evaluations").glob("*/attempt_*"))
        sql = read_json(attempt / "sql/output.json")
        self.assertEqual(sql["intermediate_reward_type"], "incomplete")
        self.assertEqual(sql["verify_result"], {"initial": 10, "final": 80})
        req = read_json(attempt / "official_request.json")
        self.assertIn('"is_final": true', req["messages"][1]["content"])
        self.assertIn('"content": "80"', req["messages"][1]["content"])
        verify_seal(folder)
        before = file_hash(folder / "events.jsonl")
        await evaluate_episode(folder, self.task, self.config, self.budget, self.sem)
        self.assertEqual(before, file_hash(folder / "events.jsonl"))
        self.assertEqual(len(list((folder / "evaluations").glob("*/attempt_*"))), 1)

    async def test_invalid_outputs_do_not_execute_and_can_recover(self):
        bad = json.dumps(call("write_value", value=99)) + json.dumps(finish())
        result, folder = await self.episode([bad, LIST, call("unknown"), call("write_value", value="bad"), finish()])
        self.assertEqual(result["tool_calls"], 0, result)
        self.assertEqual(result["termination_reason"], "finish", result)
        self.assertEqual(sum(e["event_type"] == "protocol_error" for e in rows(folder / "events.jsonl")), 3)

    async def test_write_timeout_preserves_committed_state_without_replay(self):
        result, folder = await self.episode([LIST, call("write_value", value=80), call("write_value", value=81, delay=8), finish()], tool_timeout=.3)
        self.assertEqual(result["termination_reason"], "tool_timeout", result)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["execution_integrity"], "outcome_unknown")
        self.assertIsNone(result["reward"])
        with sqlite3.connect(folder / "final.db") as db:
            self.assertEqual(db.execute("select value from items").fetchone()[0], 81)
        self.assertEqual(sum(e["event_type"] == "tool_started" for e in rows(folder / "events.jsonl")), 2)

    async def test_four_candidates_have_independent_worlds(self):
        results = await asyncio.gather(*(self.episode([LIST, call("write_value", value=80 + i), finish()], name=f"candidate{i}") for i in range(4)))
        for i, (result, folder) in enumerate(results):
            self.assertEqual(result["termination_reason"], "finish", result)
            with sqlite3.connect(folder / "initial.db") as db:
                self.assertEqual(db.execute("select value from items").fetchone()[0], 10)
            with sqlite3.connect(folder / "final.db") as db:
                self.assertEqual(db.execute("select value from items").fetchone()[0], 80 + i)

    async def test_filesystem_sandbox_cannot_read_hidden_or_other_world(self):
        hidden = self.root / "secret.txt"
        hidden.write_text("HIDDEN_SENTINEL")
        result, folder = await self.episode([LIST, call("probe_path", path=str(hidden)),
                                           call("probe_path", path=self.task.initial_state_ref), finish()])
        self.assertEqual(result["termination_reason"], "finish", result)
        trace = read_json(folder / "trajectory.json")
        self.assertNotIn("HIDDEN_SENTINEL", json.dumps(trace))
        for item in trace["trajectory"][1:3]:
            self.assertIn('readable', item["tool_response"]["content"])
            self.assertIn('false', item["tool_response"]["content"])

    async def test_context_and_turn_limit_keep_snapshot(self):
        result, folder = await self.episode([LIST], context_limit=100)
        self.assertEqual(result["termination_reason"], "context_limit", result)
        self.assertEqual(result["terminal_state_status"], "trusted", result)
        result, _ = await self.episode([LIST], name="turn_limit", max_turns=1)
        self.assertEqual(result["termination_reason"], "max_turns", result)

    async def test_judge_truncation_is_pending(self):
        result, folder = await self.episode([finish("answer")], mock_finish_reason="length")
        self.assertEqual(result["evaluation_status"], "pending", result)
        self.assertIsNone(result["reward"])

    async def test_sql_error_is_pending(self):
        self.task = fixture(self.root / "bad_sql", "def verify_task(initial_db_path, final_db_path):\n    raise RuntimeError('SQL failure')")
        result, _ = await self.episode([finish()])
        self.assertEqual(result["evaluation_status"], "pending", result)

    async def test_service_exit_keeps_uncertainty(self):
        result, _ = await self.episode([LIST, call("crash"), finish()])
        self.assertEqual(result["execution_integrity"], "outcome_unknown", result)
        self.assertIsNone(result["reward"])

    async def test_same_database_different_answer_changes_evaluation_key(self):
        runner = EpisodeRunner(self.config, self.budget, self.sem)
        # Deliberately keep rollout/attempt/task/DB/config identical; only answer differs.
        a = await runner.run(self.task, self.root / "a", "same-rollout", ScriptedBackend([finish("correct")]))
        b = await runner.run(self.task, self.root / "b", "same-rollout", ScriptedBackend([finish("wrong")]))
        self.assertEqual(file_hash(self.root / "a/final.db"), file_hash(self.root / "b/final.db"))
        self.assertIsNotNone(a["evaluation_id"], a)
        self.assertNotEqual(a["evaluation_id"], b["evaluation_id"])

    async def test_long_observation_is_only_visible_after_read(self):
        class Reader:
            async def generate(inner, messages, folder, turn):
                if turn == 1:
                    action = LIST
                elif turn == 2:
                    action = call("long_result")
                elif turn == 3:
                    observation = json.loads(messages[-1]["content"])
                    self.assertTrue(observation["truncated"])
                    self.assertNotIn("LAST_RECORD", json.dumps(messages))
                    action = {"type": "read_observation", "observation_id": observation["observation_id"],
                              "offset": observation["total_chars"] - 500, "limit": 500}
                else:
                    self.assertIn("LAST_RECORD", messages[-1]["content"])
                    action = finish("read final record")
                return {"content": json.dumps(action), "finish_reason": "stop", "test_only": True, "usage": None}
        result = await EpisodeRunner(self.config, self.budget, self.sem).run(
            self.task, self.root / "long", "long", Reader())
        self.assertEqual(result["termination_reason"], "finish", result)
        events = rows(self.root / "long/events.jsonl")
        self.assertTrue(any(e.get("truncated") for e in events))

    async def test_cancel_during_write_settles_without_replaying(self):
        class Cancelling(ScriptedBackend):
            async def generate(inner, messages, folder, turn):
                if turn == 2:
                    current = asyncio.current_task()
                    asyncio.get_running_loop().call_later(.15, current.cancel)
                return await super().generate(messages, folder, turn)
        result = await EpisodeRunner(self.config, self.budget, self.sem).run(
            self.task, self.root / "cancel", "cancel",
            Cancelling([LIST, call("write_value", value=99, delay=8), finish()]))
        self.assertEqual(result["termination_reason"], "cancelled", result)
        self.assertEqual(result["execution_integrity"], "outcome_unknown", result)
        self.assertIsNone(result["reward"])

    async def test_sql_worker_has_no_hidden_file_access(self):
        hidden = self.root / "private_label.txt"
        hidden.write_text("reference label")
        self.task = fixture(self.root / "private_sql", "def verify_task(initial_db_path, final_db_path):\n    import os\n    return {'private_visible': os.path.exists(" + repr(str(hidden)) + ")}")
        result, folder = await self.episode([finish()])
        self.assertEqual(result["evaluation_status"], "test_only", result)
        output = next((folder / "evaluations").glob("*/attempt_*/sql/output.json"))
        self.assertFalse(read_json(output)["verify_result"]["private_visible"])


if __name__ == "__main__":
    unittest.main()
