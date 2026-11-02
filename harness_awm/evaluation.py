import asyncio
import json
from pathlib import Path
from .backend import api_request
from .environment import worker
from .io import digest, file_hash, read_json, snapshot, verify_seal, write_json


def judge_config_hash(config):
    return digest({"api": config.judge_api.model_dump(), "official_verify": file_hash(Path(config.official_root) / "awm/core/verify.py"),
                   "protocol": config.protocol_id, "reward_version": "official-binary-v1"})


def mock_response(classification="incomplete", finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps({
        "classification": classification, "reasoning": "OFFLINE TEST SUBSTITUTE; no semantic judgment performed",
        "confidence_score": [0, 0, 0, 0], "evidence": {}})}}], "usage": None}


def reward_for(classification, *, evaluation_valid, execution_valid, approved, test_only):
    valid = evaluation_valid and execution_valid and approved and not test_only and classification in {"complete", "incomplete", "agent_error"}
    return {"reward_valid": bool(valid), "reward": int(classification == "complete") if valid else None}


async def evaluate_episode(folder, task, config, budget, semaphore):
    folder = Path(folder)
    verify_seal(folder)
    result = read_json(folder / "episode_result.json")
    trace = read_json(folder / "trajectory.json")
    verifier = read_json(task.verifier_ref)
    if digest(verifier) != task.verifier_hash:
        raise ValueError("verifier_hash_mismatch")
    key = digest({"rollout": result["rollout_id"], "attempt": result["attempt_id"],
                  "initial": file_hash(folder / "initial.db"), "final": file_hash(folder / "final.db"),
                  "trajectory": digest(trace), "answer": digest(trace["final_answer"]),
                  "verifier": task.verifier_hash, "judge": judge_config_hash(config),
                  "judge_backend": config.judge, "mock_classification": config.mock_classification,
                  "mock_finish_reason": config.mock_finish_reason, "reward_version": "official-binary-v1"})
    root = folder / "evaluations" / key
    root.mkdir(parents=True, exist_ok=True)
    existing = sorted(root.glob("attempt_*/result.json"))
    for path in existing:
        prior = read_json(path)
        if prior["evaluation_valid"]:
            return prior  # First valid classification is immutable, including incomplete and mock.
    attempt = root / f"attempt_{len(existing):04d}"
    # Interrupted workers also consume an attempt; never overwrite one.
    while attempt.exists():
        attempt = root / f"attempt_{int(attempt.name.split('_')[1]) + 1:04d}"
    attempt.mkdir()
    outcome = {"evaluation_id": key, "evaluation_valid": False, "evaluation_status": "pending",
               "outcome_classification": None, "official_pass": None, "test_only": config.judge == "mock" or result["test_only"],
               "reward_valid": False, "reward": None, "attempt": str(attempt.relative_to(folder))}
    try:
        async with semaphore:
            sql_folder = attempt / "sql"
            sql_folder.mkdir()
            initial, final = sql_folder / "initial.db", sql_folder / "final.db"
            snapshot(folder / "initial.db", initial)
            snapshot(folder / "final.db", final)
            before = (file_hash(initial), file_hash(final))
            sql = await worker(config, "sql", sql_folder, {"verifier": verifier, "initial": str(initial.resolve()), "final": str(final.resolve())})
            if before != (file_hash(initial), file_hash(final)):
                raise ValueError("verifier_modified_snapshot_copy")
            if sql["intermediate_reward_type"] == "judge_error":
                raise ValueError("sql_verifier_error")
            payload = {"task": task.instruction, "sql": sql["verify_result"], "trajectory": trace["trajectory"],
                       "base_url": config.judge_api.base_url, "model": config.judge_api.model,
                       "response": mock_response(config.mock_classification, config.mock_finish_reason)}
            built = await worker(config, "judge", attempt / "request_build", payload)
            request = built["request"]
            if not request:
                raise ValueError("official_judge_request_not_built")
            write_json(attempt / "official_request.json", request)
            # No answer injection: assert the official builder received our real final record.
            if trace["finished"] and not any(r.get("is_final") and r.get("content") == trace["final_answer"] for r in trace["trajectory"]):
                raise ValueError("missing_final_answer_in_trajectory")
            if config.judge == "api":
                response = await api_request(config.judge_api, request, budget,
                    f"{result['rollout_id']}/judge/{key}/{attempt.name}", "judge", attempt / "transport")
                payload["response"] = response
                parsed = await worker(config, "judge", attempt / "response_parse", payload)
                if parsed["request"] != request:
                    raise ValueError("official_request_changed_during_parse")
            else:
                response, parsed = payload["response"], built
            write_json(attempt / "raw_response.json", response)
            classification = parsed["classification"]
            finish = response["choices"][0]["finish_reason"]
            # The official parser is permissive; validity is a separate harness contract.
            content = response["choices"][0]["message"]["content"]
            strict = json.loads(content)
            valid = finish == "stop" and isinstance(strict, dict) and strict.get("classification") == classification and classification in {"complete", "incomplete", "agent_error", "server_error"}
            outcome.update(outcome_classification=classification, evaluation_valid=valid,
                           evaluation_status=("test_only" if outcome["test_only"] else "valid") if valid else "pending",
                           official_pass=(classification == "complete") if valid and config.judge == "api" else None,
                           finish_reason=finish, judge_details=parsed["details"])
            approved = (task.readiness_profile.get("reward_ready") is True and
                        task.judge_config_hash == judge_config_hash(config) and
                        task.tool_schema_hash is not None and config.isolation == "strict")
            outcome.update(reward_for(classification, evaluation_valid=valid,
                execution_valid=result["execution_integrity"] == "valid" and result["terminal_state_status"] == "trusted",
                approved=approved, test_only=outcome["test_only"]))
            if not approved:
                outcome["reward_exclusion"] = "task_or_judge_configuration_not_admitted"
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    verify_seal(folder)
    write_json(attempt / "result.json", outcome)
    return outcome


def apply_evaluation(folder, evaluation):
    path = Path(folder) / "episode_result.json"
    result = read_json(path)
    for key in ("evaluation_id", "evaluation_status", "outcome_classification", "official_pass", "reward_valid", "reward", "test_only"):
        result[key] = evaluation[key]
    result["lifecycle"] = "EVAL_PENDING" if evaluation["evaluation_status"] == "pending" else "SETTLED"
    write_json(path, result)
    return result
