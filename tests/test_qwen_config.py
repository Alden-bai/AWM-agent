import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_awm.backend import Budget, api_request
from harness_awm.qwen import load_qwen_config


class QwenTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_wire_and_preflight(self):
        calls = []

        class FakeClient:
            def __init__(self, **settings):
                self.chat = SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(model_dump=lambda **_: {"usage": {"total_tokens": 30}})

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake-key"}), patch("openai.AsyncOpenAI", FakeClient):
            config = load_qwen_config()
            self.assertEqual(config.context_limit, 128000)
            self.assertEqual(config.max_physical_requests, 0)
            self.assertEqual(config.actor_api, config.judge_api)
            budget = Budget(Path(tmp) / "budget.db", 3, 300000)
            kwargs = {"model": config.actor_api.model,
                      "messages": [{"role": "user", "content": "hello"}],
                      "max_completion_tokens": 4096,
                      "extra_body": {"thinking": {"type": "disabled"}, "enable_thinking": True}}
            await api_request(config.actor_api, kwargs, budget, "ok", "actor", Path(tmp) / "ok")
            self.assertEqual(calls[0]["extra_body"], {"enable_thinking": False})
            self.assertEqual(calls[0]["max_tokens"], 4096)
            self.assertNotIn("max_completion_tokens", calls[0])
            self.assertTrue(kwargs["extra_body"]["enable_thinking"])
            oversized = {**kwargs, "messages": [{"role": "user", "content": "a" * 128000}]}
            with self.assertRaisesRegex(ValueError, "api_context_limit_exceeded"):
                await api_request(config.actor_api, oversized, budget, "long", "judge", Path(tmp) / "long")
            self.assertEqual(len(calls), 1)
            self.assertEqual(budget.summary()["physical_requests"], 1)
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "missing_api_key"):
                    await api_request(config.actor_api, kwargs, budget, "empty", "actor", Path(tmp) / "empty")

    def test_env_file_loading_preserves_exported_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("DASHSCOPE_API_KEY=file-key\n")
            config = load_qwen_config().model_dump()
            with patch("harness_awm.qwen.ROOT", root), patch("harness_awm.qwen.read_json", return_value=config), patch.dict(os.environ, {}, clear=True):
                load_qwen_config()
                self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "file-key")
                os.environ["DASHSCOPE_API_KEY"] = "exported-key"
                load_qwen_config()
                self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "exported-key")
