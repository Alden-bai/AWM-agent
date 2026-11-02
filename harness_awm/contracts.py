from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class APIConfig(Contract):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "unconfigured"
    api_key_env: str = "HARNESS_API_KEY"
    timeout: float = Field(default=120, gt=0)
    temperature: float = Field(default=1.0, ge=0)
    max_tokens_alias: bool = False
    enable_thinking: bool | None = None
    context_limit: int | None = Field(default=None, ge=1)


class RunConfig(Contract):
    schema_version: str = "awm-harness-v1"
    protocol_id: str = "awm-single-json-v1"
    official_root: str = str(ROOT / "agent-world-model")
    runtime_python: str = str(ROOT / "agent-world-model/.venv/bin/python")
    isolation: Literal["strict", "development"] = "strict"
    purpose: Literal["development", "formal"] = "development"
    actor: Literal["scripted", "api"] = "scripted"
    judge: Literal["mock", "api"] = "mock"
    actor_api: APIConfig = Field(default_factory=APIConfig)
    judge_api: APIConfig = Field(default_factory=APIConfig)
    mock_classification: Literal["complete", "incomplete", "agent_error", "server_error", "judge_error"] = "incomplete"
    mock_finish_reason: str = "stop"
    workers: int = Field(default=1, ge=1, le=32)
    model_concurrency: int = Field(default=1, ge=1)
    judge_concurrency: int = Field(default=1, ge=1)
    candidates_per_task: int = Field(default=1, ge=1)
    max_turns: int = Field(default=32, ge=1)
    max_tool_calls: int = Field(default=32, ge=1)
    episode_timeout: float = Field(default=600, gt=0)
    tool_timeout: float = Field(default=60, gt=0)
    startup_timeout: float = Field(default=60, gt=0)
    stop_timeout: float = Field(default=5, gt=0)
    evaluator_timeout: float = Field(default=120, gt=0)
    context_limit: int = Field(default=32768, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)
    safety_margin: int = Field(default=512, ge=0)
    episode_token_limit: int = Field(default=131072, ge=1)
    max_physical_requests: int = Field(default=0, ge=0)
    max_total_tokens: int = Field(default=0, ge=0)
    observation_chars: int = Field(default=8000, ge=64)
    max_read_chars: int = Field(default=4000, ge=1)
    seed: int = 0
    timezone: str = "Asia/Shanghai"
    clock_policy: Literal["actual_recorded"] = "actual_recorded"

    @model_validator(mode="after")
    def formal(self):
        if self.purpose == "formal" and (self.isolation != "strict" or self.actor != "api" or self.judge != "api"):
            raise ValueError("formal runs require strict isolation and real actor/judge")
        return self


class TaskSpec(Contract):
    schema_version: str = "awm-task-v1"
    protocol_id: str = "awm-single-json-v1"
    dataset_version: str
    split: Literal["development", "train", "dev", "test"] = "development"
    lineage_group_id: str
    scenario: str
    native_task_id: int | str
    task_instance_id: str
    instruction: str
    initial_state_ref: str
    initial_state_hash: str
    initial_state_fingerprint: str
    environment_ref: str
    environment_code_hash: str
    tool_schema_hash: str | None = None
    verifier_ref: str
    verifier_hash: str
    judge_config_hash: str | None = None
    readiness_profile: dict = Field(default_factory=lambda: {"runtime_ready": True, "reward_ready": False})
    cleaning_status: str = "development_fixture"
    source_manifest_hash: str
    world_datetime: str | None = None
    timezone: str = "Asia/Shanghai"
    clock_policy: str = "actual_recorded"
    relative_time_resolved: bool = False


class EpisodeResult(Contract):
    schema_version: str = "awm-episode-v1"
    rollout_id: str
    attempt_id: str = "a0"
    task_instance_id: str
    lifecycle: str = "SETTLED"
    termination_reason: str
    termination_origin: str = "runtime"
    execution_integrity: str = "valid"
    terminal_state_status: str = "unavailable"
    evaluation_status: str = "not_run"
    outcome_classification: str | None = None
    official_pass: bool | None = None
    reward_valid: bool = False
    reward: int | None = None
    process_status: str = "not_evaluated"
    q: None = None
    turns: int = 0
    tool_calls: int = 0
    evaluation_id: str | None = None
    test_only: bool = True
    error: str | None = None

