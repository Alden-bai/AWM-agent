import asyncio
import os
import sqlite3
from pathlib import Path
from .io import canonical, now, write_json
from .protocol import estimate_tokens


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, path, max_requests, max_tokens):
        self.path = Path(path)
        self.max_requests, self.max_tokens = max_requests, max_tokens
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, request_key TEXT UNIQUE, stage TEXT, reserved INTEGER, status TEXT, usage TEXT, started TEXT, finished TEXT)")

    def reserve(self, key, stage, messages, output_tokens):
        upper = estimate_tokens(messages) + output_tokens + 512
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            count, total = db.execute("SELECT count(*),coalesce(sum(reserved),0) FROM requests").fetchone()
            if count >= self.max_requests or total + upper > self.max_tokens:
                raise BudgetExceeded("global_budget_exhausted")
            cursor = db.execute("INSERT INTO requests(request_key,stage,reserved,status,started) VALUES(?,?,?,?,?)",
                                (key, stage, upper, "in_flight", now()))
            db.commit()
            return cursor.lastrowid

    def finish(self, rid, usage=None):
        with sqlite3.connect(self.path, timeout=30) as db:
            if usage and isinstance(usage.get("total_tokens"), int) and usage["total_tokens"] >= 0:
                db.execute("UPDATE requests SET reserved=?,usage=?,status='response',finished=? WHERE id=?",
                           (usage["total_tokens"], canonical(usage), now(), rid))
            else:
                db.execute("UPDATE requests SET status='usage_unknown',finished=? WHERE id=?", (now(), rid))

    def summary(self):
        with sqlite3.connect(self.path) as db:
            count, total = db.execute("SELECT count(*),coalesce(sum(reserved),0) FROM requests").fetchone()
            unknown = db.execute("SELECT count(*) FROM requests WHERE status!='response'").fetchone()[0]
        return {"physical_requests": count, "tokens_accounted_or_reserved": total, "unknown_usage_requests": unknown}


async def api_request(config, kwargs, budget, key, stage, folder):
    from openai import AsyncOpenAI
    if not os.environ.get(config.api_key_env, "").strip():
        raise ValueError("missing_api_key_environment_variable: " + config.api_key_env)
    wire = dict(kwargs)
    if config.max_tokens_alias and "max_completion_tokens" in wire:
        wire["max_tokens"] = wire.pop("max_completion_tokens")
    if config.enable_thinking is not None:
        extra = dict(wire.get("extra_body") or {})
        extra.pop("thinking", None)  # DeepSeek's switch is not a Qwen parameter.
        extra["enable_thinking"] = config.enable_thinking
        wire["extra_body"] = extra
    if config.context_limit is not None:
        output_tokens = wire.get("max_completion_tokens", wire.get("max_tokens", 0))
        if estimate_tokens(wire["messages"]) + output_tokens + 512 > config.context_limit:
            raise ValueError("api_context_limit_exceeded")
    write_json(Path(folder) / "request.json", wire)
    rid = budget.reserve(key, stage, wire["messages"], wire.get("max_completion_tokens", wire.get("max_tokens", 0)))
    try:
        async with AsyncOpenAI(base_url=config.base_url, api_key=os.environ[config.api_key_env],
                               max_retries=0, timeout=config.timeout) as client:
            response = await client.chat.completions.create(**wire)
        data = response.model_dump(mode="json")
        write_json(Path(folder) / "response.json", data)
        budget.finish(rid, data.get("usage"))
        return data
    except BaseException as exc:
        budget.finish(rid)
        # Preserve provider failure evidence without persisting Authorization headers.
        error = {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
                 "body": getattr(exc, "body", None), "usage_status": "unknown_reserved"}
        secret = os.environ.get(config.api_key_env, "")
        def redact(value):
            if isinstance(value, str):
                return value.replace(secret, "[REDACTED]") if secret else value
            if isinstance(value, dict):
                return {str(k): redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return redact(str(value))
        write_json(Path(folder) / "error.json", redact(error))
        raise


class ScriptedBackend:
    """Actions supplied by the operator; no access to private runtime evidence."""
    def __init__(self, actions):
        self.actions = list(actions)
        self.index = 0

    async def generate(self, messages, folder, turn):
        if self.index >= len(self.actions):
            raise RuntimeError("script_exhausted")
        item = self.actions[self.index]
        self.index += 1
        if isinstance(item, dict) and "raw_output" in item:
            content = item["raw_output"]
            reason = item.get("finish_reason", "stop")
        else:
            content, reason = (item if isinstance(item, str) else canonical(item)), "stop"
        return {"content": content, "finish_reason": reason, "test_only": True,
                "model_version": "scripted-v1", "usage": None}


class APIBackend:
    def __init__(self, config, budget, semaphore, rollout_id):
        self.config, self.budget, self.semaphore, self.rollout_id = config, budget, semaphore, rollout_id

    async def generate(self, messages, folder, turn):
        c = self.config
        kwargs = {"model": c.actor_api.model, "messages": messages,
                  "temperature": c.actor_api.temperature, "max_completion_tokens": c.max_output_tokens}
        async with self.semaphore:
            data = await api_request(c.actor_api, kwargs, self.budget, f"{self.rollout_id}/actor/{turn}", "actor", folder)
        choice = data["choices"][0]
        return {"content": choice["message"].get("content") or "", "finish_reason": choice["finish_reason"],
                "test_only": False, "usage": data.get("usage"), "model_version": data.get("model"),
                "system_fingerprint": data.get("system_fingerprint")}


class TrainingActorBackend:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("P4 training actor/token-logprob contract is not implemented")


class ProcessEvaluator:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("P5 process rubric calibration is not implemented")

