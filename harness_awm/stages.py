import asyncio
import platform
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from .backend import APIBackend, Budget, ScriptedBackend
from .contracts import TaskSpec
from .evaluation import apply_evaluation, evaluate_episode, judge_config_hash
from .io import canonical, digest, file_hash, now, read_json, rows, verify_seal, write_json
from .registry import load_registry
from .runner import EpisodeRunner


def official_hashes(config):
    root = Path(config.official_root) / "awm"
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob("*.py"))}


def dependencies():
    versions = {}
    for name in ("mcp", "httpx", "openai", "pydantic", "jsonschema", "fastapi", "fastapi-mcp"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "missing_in_controller"
    return {"python": platform.python_version(), "packages": versions}


def metrics(run):
    run = Path(run)
    manifest = read_json(run / "manifest.json")
    results, terminations, invalid = [], Counter(), Counter()
    by_scenario = {}
    truncation, rereads, token_estimate, elapsed = 0, 0, 0, 0
    for candidate in manifest["candidates"]:
        folder = run / candidate["path"]
        path = folder / "episode_result.json"
        result = read_json(path) if path.exists() else {"termination_reason": "not_settled", "reward_valid": False, "reward": None}
        results.append(result)
        terminations[result["termination_reason"]] += 1
        if not result["reward_valid"]:
            invalid[result.get("evaluation_status", "not_settled") + ":" + result.get("execution_integrity", "unknown")] += 1
        bucket = by_scenario.setdefault(candidate["scenario"], {"planned": 0, "valid": 0, "successful": 0})
        bucket["planned"] += 1
        bucket["valid"] += int(result["reward_valid"])
        bucket["successful"] += int(result.get("reward") == 1 and result["reward_valid"])
        if (folder / "events.jsonl").exists():
            for event in rows(folder / "events.jsonl"):
                truncation += int(event["event_type"] == "observation_delivered" and event.get("truncated", False))
                rereads += int(event.get("parsed_action", {}).get("type") == "read_observation")
                token_estimate += event.get("token_count_or_estimate", 0)
                elapsed += event.get("elapsed_seconds", 0)
    valid = sum(r["reward_valid"] for r in results)
    success = sum(r["reward_valid"] and r.get("reward") == 1 for r in results)
    total = len(results)
    data = {"planned_tasks": len({c["task_instance_id"] for c in manifest["candidates"]}),
            "planned_candidates": total, "settled_candidates": sum(r.get("lifecycle") == "SETTLED" for r in results),
            "valid_settlements": valid, "valid_successes": success,
            "official_evaluated_candidates": sum(r.get("official_pass") is not None for r in results),
            "official_passes": sum(r.get("official_pass") is True for r in results),
            "test_only_candidates": sum(r.get("test_only", False) for r in results),
            "success_over_valid": {"numerator": success, "denominator": valid, "rate": success / valid if valid else None},
            "success_over_planned": {"numerator": success, "denominator": total, "rate": success / total if total else None},
            "invalid_count": total - valid, "invalid_reasons": dict(invalid), "termination_reasons": dict(terminations),
            "by_scenario": by_scenario, "context_limit_count": terminations["context_limit"],
            "observation_truncations": truncation, "observation_reads": rereads,
            "tokens_reported_or_estimated": token_estimate, "episode_seconds_sum": elapsed,
            "interpretation": "invalid is not confirmed business failure; mock outcomes are not official successes"}
    if (run / "budget.sqlite").exists():
        data["budget"] = Budget(run / "budget.sqlite", 0, 0).summary()
    write_json(run / "metrics.json", data)
    return data


async def run_stage(stage, registry, run, config, scripts=None, task_ids=None):
    if stage not in {"collect", "evaluate"}:
        raise NotImplementedError("only P0-P3 collect/evaluate are implemented")
    tasks, registry_manifest = load_registry(registry)
    if task_ids:
        requested = set(task_ids)
        tasks = [t for t in tasks if t.task_instance_id in requested or f"{t.scenario}:{t.native_task_id}" in requested]
        if len(tasks) != len(requested):
            raise ValueError("unknown_or_duplicate_task_selection")
    if not tasks:
        raise ValueError("no_tasks")
    if config.purpose == "formal":
        for task in tasks:
            if task.split == "development" or (stage == "collect" and task.split != "train"):
                raise ValueError("formal_stage_split_not_admitted")
            if not task.tool_schema_hash or not task.relative_time_resolved:
                raise ValueError("formal_tools_or_time_not_admitted")
            if not task.readiness_profile.get("reward_ready") or task.judge_config_hash != judge_config_hash(config):
                raise ValueError("formal_reward_configuration_not_admitted")
    scripts_data = read_json(scripts) if scripts else {}
    if config.actor == "scripted" and not scripts:
        raise ValueError("scripted_actor_requires_scripts_file")
    run = Path(run).resolve()
    run.mkdir(parents=True, exist_ok=False)
    candidates = []
    for ti, task in enumerate(tasks):
        for ci in range(config.candidates_per_task):
            rid = f"t{ti:04d}_c{ci:03d}"
            candidates.append({"rollout_id": rid, "group_id": f"t{ti:04d}", "candidate_id": ci,
                               "task_instance_id": task.task_instance_id, "scenario": task.scenario,
                               "path": f"episodes/{rid}/a0"})
    manifest = {"schema_version": "awm-run-v1", "run_id": run.name, "created_at": now(), "stage": stage,
                "config": config.model_dump(), "registry": str(Path(registry).resolve()),
                "registry_hash": digest(registry_manifest), "tasks": [t.model_dump() for t in tasks],
                "candidates": candidates, "scripts": scripts_data, "official_source_hashes": official_hashes(config),
                "harness_source_hashes": {p.name: file_hash(p) for p in Path(__file__).parent.glob('*.py')},
                "dependencies": dependencies()}
    write_json(run / "manifest.json", manifest)
    write_json(run / "manifest.sha256.json", {"sha256": file_hash(run / "manifest.json")})
    budget = Budget(run / "budget.sqlite", config.max_physical_requests, config.max_total_tokens)
    worker_sem = asyncio.Semaphore(config.workers)
    model_sem, judge_sem = asyncio.Semaphore(config.model_concurrency), asyncio.Semaphore(config.judge_concurrency)
    task_map = {t.task_instance_id: t for t in tasks}
    runner = EpisodeRunner(config, budget, judge_sem)

    async def candidate_job(candidate):
        async with worker_sem:
            task = task_map[candidate["task_instance_id"]]
            rid = candidate["rollout_id"]
            if config.actor == "scripted":
                actions = scripts_data.get(f"{task.scenario}:{task.native_task_id}", scripts_data.get("*", []))
                if isinstance(actions, dict):
                    actions = actions.get(str(candidate["candidate_id"]), actions.get("*", []))
                backend = ScriptedBackend(actions)
            else:
                backend = APIBackend(config, budget, model_sem, rid)
            try:
                return await runner.run(task, run / candidate["path"], rid, backend)
            except Exception as exc:
                write_json(run / "failures" / (rid + ".json"), {"error": f"{type(exc).__name__}: {exc}"})
                return None
    try:
        await asyncio.gather(*(candidate_job(c) for c in candidates))
    finally:
        report = metrics(run)
    return report


def check_manifest(run):
    run = Path(run)
    if file_hash(run / "manifest.json") != read_json(run / "manifest.sha256.json")["sha256"]:
        raise ValueError("run_manifest_modified")
    return read_json(run / "manifest.json")


async def reevaluate(run):
    from .contracts import RunConfig
    run = Path(run)
    manifest = check_manifest(run)
    config = RunConfig.model_validate(manifest["config"])
    if official_hashes(config) != manifest["official_source_hashes"]:
        raise ValueError("official_source_changed")
    budget = Budget(run / "budget.sqlite", config.max_physical_requests, config.max_total_tokens)
    sem = asyncio.Semaphore(config.judge_concurrency)
    tasks = {t["task_instance_id"]: TaskSpec.model_validate(t) for t in manifest["tasks"]}
    for candidate in manifest["candidates"]:
        folder = run / candidate["path"]
        if not (folder / "episode_result.json").exists():
            continue
        result = read_json(folder / "episode_result.json")
        if result["terminal_state_status"] == "trusted":
            evaluation = await evaluate_episode(folder, tasks[candidate["task_instance_id"]], config, budget, sem)
            apply_evaluation(folder, evaluation)
    return metrics(run)


def diagnose(run):
    run = Path(run)
    manifest = check_manifest(run)
    findings = []
    for candidate in manifest["candidates"]:
        folder = run / candidate["path"]
        item = {"rollout_id": candidate["rollout_id"], "issues": []}
        try:
            verify_seal(folder)
        except Exception as exc:
            item["issues"].append("evidence_invalid: " + str(exc))
        try:
            events = rows(folder / "events.jsonl")
            previous, outstanding = None, set()
            for seq, event in enumerate(events, 1):
                if event["seq"] != seq or event["parent_event_id"] != previous:
                    item["issues"].append("event_chain_broken")
                previous = event["event_id"]
                if event["event_type"] == "tool_started":
                    outstanding.add(event["event_id"])
                if event["event_type"] == "tool_completed":
                    outstanding.discard(event["call_event_id"])
            if outstanding:
                item["issues"].append("outcome_unknown; do not replay writes")
        except Exception as exc:
            item["issues"].append("events_unreadable: " + str(exc))
        if (folder / "episode_result.json").exists():
            item["result"] = read_json(folder / "episode_result.json")
        findings.append(item)
    return {"run": str(run.resolve()), "read_only": True, "episodes": findings}
