import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harness_awm.backend import Budget, BudgetExceeded, api_request
from harness_awm.contracts import APIConfig
from harness_awm.export import export_sft
from harness_awm.io import Recorder, file_hash, read_json, seal, write_json
from harness_awm.stages import diagnose, metrics


class APITransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_transport_retains_reservation_and_redacts_secret(self):
        class BrokenClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def create(self, **kwargs):
                exc = RuntimeError("provider failed")
                exc.body = {"echo": 'quoted"secret'}
                raise exc
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TEST_HARNESS_KEY": 'quoted"secret'}), patch("openai.AsyncOpenAI", BrokenClient):
            config = APIConfig(api_key_env="TEST_HARNESS_KEY")
            budget = Budget(Path(tmp) / "budget.db", 1, 10000)
            with self.assertRaises(RuntimeError):
                await api_request(config, {"model": "test", "messages": [], "max_completion_tokens": 10},
                                  budget, "failure", "actor", Path(tmp) / "call")
            self.assertEqual(budget.summary()["unknown_usage_requests"], 1)
            self.assertGreater(budget.summary()["tokens_accounted_or_reserved"], 0)
            error = read_json(Path(tmp) / "call/error.json")
            self.assertEqual(error["body"]["echo"], "[REDACTED]")

    async def test_zero_budget_never_dispatches_and_alias_is_explicit(self):
        calls = []
        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)
                self.settings = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def create(self, **kwargs):
                calls.append((self.settings, kwargs))
                return SimpleNamespace(model_dump=lambda **_: {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {"total_tokens": 30}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TEST_HARNESS_KEY": "TEST_SECRET"}), patch("openai.AsyncOpenAI", FakeClient):
            config = APIConfig(api_key_env="TEST_HARNESS_KEY", max_tokens_alias=True)
            kwargs = {"model": "test", "messages": [{"role": "user", "content": "hello"}], "max_completion_tokens": 100}
            budget = Budget(Path(tmp) / "closed.db", 0, 0)
            with self.assertRaises(BudgetExceeded):
                await api_request(config, kwargs, budget, "a", "actor", Path(tmp) / "closed")
            self.assertEqual(calls, [])
            budget = Budget(Path(tmp) / "open.db", 1, 10000)
            await api_request(config, kwargs, budget, "a", "actor", Path(tmp) / "open")
            self.assertEqual(calls[0][0]["max_retries"], 0)
            self.assertEqual(calls[0][1]["max_tokens"], 100)
            self.assertNotIn("max_completion_tokens", calls[0][1])
            self.assertEqual(budget.summary()["physical_requests"], 1)
            self.assertEqual(budget.summary()["tokens_accounted_or_reserved"], 30)
            self.assertNotIn("TEST_SECRET", (Path(tmp) / "open/request.json").read_text())


def export_fixture(root, test_only=False, gap=False):
    """Synthetic trusted evidence to exercise exporter gates, never a training run."""
    root = Path(root)
    ep = root / "episodes" / "one" / "a0"
    ep.mkdir(parents=True)
    manifest = {"config": {"purpose": "formal"}, "tasks": [{"task_instance_id": "task1", "split": "train"}],
                "candidates": [{"rollout_id": "one", "task_instance_id": "task1", "scenario": "fixture", "path": "episodes/one/a0"},
                               {"rollout_id": "missing", "task_instance_id": "task1", "scenario": "fixture", "path": "episodes/missing/a0"}]}
    write_json(root / "manifest.json", manifest)
    write_json(root / "manifest.sha256.json", {"sha256": file_hash(root / "manifest.json")})
    recorder = Recorder(ep, "one")
    event_id = recorder.emit("tool_started", tool_name="write")
    if not gap:
        recorder.emit("tool_completed", call_event_id=event_id)
    write_json(ep / "model_turns/0001/input.json", {"messages": [{"role": "user", "content": "public instruction"}]})
    write_json(ep / "model_turns/0001/output.json", {"content": '{"type":"finish","answer":"done"}',
                                                       "finish_reason": "stop", "test_only": test_only})
    write_json(ep / "episode_result.json", {"lifecycle": "SETTLED", "termination_reason": "finish", "reward_valid": True,
                 "reward": 1, "test_only": test_only, "execution_integrity": "valid", "terminal_state_status": "trusted", "turns": 1, "evaluation_id": "evaluation"})
    write_json(ep / "evaluations/evaluation/attempt_0000/result.json", {
        "evaluation_valid": True, "reward_valid": True, "reward": 1, "outcome_classification": "complete", "test_only": test_only, "private_label": "PRIVATE_EVAL_SENTINEL"})
    write_json(ep / "audit/hidden.json", {"hidden": "PRIVATE_DB_SENTINEL"})
    seal(ep)
    return ep


class StageTests(unittest.TestCase):
    def test_export_has_only_original_public_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            ep = export_fixture(run)
            report = export_sft(run, Path(tmp) / "export")
            self.assertEqual(report["accepted_episodes"], 1)
            self.assertEqual(report["exported_turns"], 1)
            text = (Path(tmp) / "export/sft.jsonl").read_text()
            self.assertIn("public instruction", text)
            self.assertNotIn("PRIVATE_", text)
            self.assertNotIn("audit", text)

    def test_mock_and_tampered_evidence_cannot_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            ep = export_fixture(run, test_only=True)
            report = export_sft(run, Path(tmp) / "export")
            self.assertEqual(report["accepted_episodes"], 0)
            write_json(ep / "model_turns/0001/output.json", {"content": "tampered"})
            report = export_sft(run, Path(tmp) / "export2")
            self.assertEqual(report["accepted_episodes"], 0)

    def test_metrics_keep_missing_candidate_and_diagnose_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            export_fixture(run, gap=True)
            report = metrics(run)
            self.assertEqual(report["planned_candidates"], 2)
            self.assertEqual(report["success_over_planned"]["denominator"], 2)
            self.assertEqual(report["invalid_count"], 1)
            before = {str(p): file_hash(p) for p in run.rglob("*") if p.is_file()}
            findings = diagnose(run)
            after = {str(p): file_hash(p) for p in run.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertIn("outcome_unknown", findings["episodes"][0]["issues"][0])
            report = export_sft(run, Path(tmp) / "export")
            self.assertEqual(report["accepted_episodes"], 0)


if __name__ == "__main__":
    unittest.main()
