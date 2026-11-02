import json
from pathlib import Path
from .contracts import TaskSpec
from .environment import worker
from .io import canonical, db_check, db_fingerprint, digest, file_hash, read_json, rows, snapshot, write_json


def check_splits(tasks):
    groups = {}
    worlds = {}
    for task in tasks:
        for mapping, key in ((groups, task.lineage_group_id), (worlds, task.initial_state_fingerprint)):
            if key in mapping and mapping[key] != task.split:
                raise ValueError("lineage_or_initial_world_crosses_split")
            mapping[key] = task.split


def load_registry(path):
    path = Path(path).resolve()
    manifest = read_json(path / "registry_manifest.json")
    for name, expected in manifest["files"].items():
        p = (path / name).resolve()
        if not p.is_relative_to(path) or file_hash(p) != expected:
            raise ValueError("registry_hash_mismatch: " + name)
    tasks = [TaskSpec.model_validate(t) for t in rows(path / "tasks.jsonl")]
    if len({t.task_instance_id for t in tasks}) != len(tasks):
        raise ValueError("duplicate_task_instance_id")
    check_splits(tasks)
    return tasks, manifest


async def prepare(source, output, config, splits=None, admissions=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    checksums = read_json(source / "artifact_checksums.json")
    for name in ("manifest.json", "tasks.keep.jsonl", "keep_audit.jsonl"):
        if file_hash(source / name) != checksums[name]:
            raise ValueError("cleaning_artifact_hash_mismatch: " + name)
    manifest, kept = read_json(source / "manifest.json"), rows(source / "tasks.keep.jsonl")
    audits = {r["ledger_id"]: r for r in rows(source / "keep_audit.jsonl")}
    names = {r["scenario"] for r in kept}
    assets = {}
    for filename in ("gen_tasks.jsonl", "gen_db.jsonl", "gen_sample.jsonl", "gen_envs.jsonl", "gen_verifier.jsonl"):
        info = manifest["files"][filename]
        path = Path(info["path"])
        if file_hash(path) != info["sha256"]:
            raise ValueError("raw_asset_hash_mismatch: " + filename)
        with path.open() as f:
            assets[filename] = [r for line in f if (r := json.loads(line))["scenario"] in names]
    def one(filename, scenario, task_id=None):
        matches = [r for r in assets[filename] if r["scenario"] == scenario and
                   (task_id is None or r.get("task_idx") == task_id)]
        # Identical verifier duplicates can be deduplicated, conflicting ones cannot.
        unique = {canonical(r): r for r in matches}
        if len(unique) != 1:
            raise ValueError("ambiguous_asset: " + filename + ":" + scenario)
        return next(iter(unique.values()))
    output.mkdir(parents=True, exist_ok=False)
    split_map = read_json(splits) if splits else {}
    admission_map = read_json(admissions) if admissions else {}
    prepared_envs, tasks = {}, []
    for row in kept:
        scenario, task_id = row["scenario"], row["task_idx"]
        if not audits[row["ledger_id"]]["passed"]:
            raise ValueError("keep_audit_failed")
        instruction = one("gen_tasks.jsonl", scenario)["tasks"][task_id]
        verifier = one("gen_verifier.jsonl", scenario, task_id)
        if instruction != row["task"] or verifier["task"] != instruction:
            raise ValueError("task_verifier_binding_mismatch")
        if __import__("hashlib").sha256(verifier["verification"]["code"].encode()).hexdigest() != row["binding"]["code_sha256"]:
            raise ValueError("verifier_code_binding_mismatch")
        if scenario not in prepared_envs:
            env_folder = output / "environments" / scenario
            env_folder.mkdir(parents=True)
            initialized = await worker(config, "initialize", env_folder / "build", {
                "scenario": scenario, "schema": one("gen_db.jsonl", scenario)["db_schema"],
                "sample": one("gen_sample.jsonl", scenario)["sample_data"]})
            if not initialized["complete"]:
                raise ValueError("initialization_incomplete: " + scenario)
            rebuilt = Path(initialized["path"])
            db_check(rebuilt)
            original = source / "environments" / scenario / "initial.db"
            db_check(original)
            matches = db_fingerprint(rebuilt) == db_fingerprint(original)
            # Some official samples evaluate SQLite 'now'. The cleaning snapshot is
            # the task instance; rebuilding today must never silently replace it.
            write_json(env_folder / "initialization_validation.json", {
                "complete_rebuild": True, "matches_cleaning_snapshot": matches,
                "rebuild_fingerprint": db_fingerprint(rebuilt),
                "cleaning_fingerprint": db_fingerprint(original),
                "source_snapshot_hash": file_hash(original),
                "policy": "preserve_cleaning_snapshot; differing rebuild blocks formal admission"})
            initial = env_folder / "initial.db"
            snapshot(original, initial)
            initial.chmod(0o444)
            environment = one("gen_envs.jsonl", scenario)
            write_json(env_folder / "environment.json", environment)
            prepared_envs[scenario] = (initial, env_folder / "environment.json", environment, matches)
        initial, env_path, environment, matches = prepared_envs[scenario]
        instance = f"{manifest['revision']}:{row['ledger_id']}"
        admit = admission_map.get(instance, {})
        verifier_path = output / "verifiers" / (digest(instance)[:20] + ".json")
        write_json(verifier_path, verifier)
        task = TaskSpec(dataset_version=manifest["revision"], split=split_map.get(scenario, "development"),
            lineage_group_id=scenario + ":" + db_fingerprint(initial), scenario=scenario, native_task_id=task_id,
            task_instance_id=instance, instruction=instruction, initial_state_ref=str(initial),
            initial_state_hash=file_hash(initial), initial_state_fingerprint=db_fingerprint(initial),
            environment_ref=str(env_path), environment_code_hash=digest(environment),
            verifier_ref=str(verifier_path), verifier_hash=digest(verifier), tool_schema_hash=admit.get("tool_schema_hash"),
            judge_config_hash=admit.get("judge_config_hash"), cleaning_status="keep_finite_suite",
            readiness_profile={"runtime_ready": True, "reward_ready": admit.get("reward_ready") is True and matches,
                               "initial_rebuild_matches": matches,
                               "admission_evidence": admit.get("evidence")},
            source_manifest_hash=file_hash(source / "manifest.json"),
            relative_time_resolved=admit.get("relative_time_resolved") is True)
        tasks.append(task)
    check_splits(tasks)
    (output / "tasks.jsonl").write_text("".join(canonical(t.model_dump()) + "\n" for t in tasks))
    write_json(output / "source.json", {"source": str(source), "manifest_hash": file_hash(source / "manifest.json"),
               "splits": split_map, "admissions": admission_map, "config": config.model_dump()})
    frozen = {str(p.relative_to(output)): file_hash(p) for p in sorted(output.rglob("*"))
              if p.is_file() and "build" not in p.relative_to(output).parts}
    write_json(output / "registry_manifest.json", {"schema_version": "awm-registry-v1", "files": frozen,
               "task_count": len(tasks), "development_only": not bool(splits)})
    return {"registry": str(output), "tasks": len(tasks)}
