#!/usr/bin/env python3
"""Non-destructive AWM defect screening. Standard library only; no model calls."""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
REVISION = "dde80a0283fe781bdc51656bce57063dc5650213"
FILES = ("gen_scenario.jsonl", "gen_tasks.jsonl", "gen_db.jsonl",
         "gen_sample.jsonl", "gen_spec.jsonl", "gen_envs.jsonl", "gen_verifier.jsonl")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rows(path):
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def write_rows(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in data:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def syntax_error(code):
    if not isinstance(code, str) or not code.strip():
        return {"error": "missing_code"}
    try:
        compile(code, "<released-code>", "exec")
    except (SyntaxError, ValueError, TypeError) as e:
        return {"error": str(e), "line": getattr(e, "lineno", None)}
    return None


def initialize(schema, sample, target=None):
    """Match official db.py/sample.py transaction and table failure boundaries.

    Executes original SQL in an isolated in-memory DB. No environment/verifier
    Python is executed. A successful DB can be exported for later MCP replay.
    """
    errors = []
    deadline = time.monotonic() + 10
    with sqlite3.connect(":memory:") as db:
        db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                          if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
                          else sqlite3.SQLITE_OK)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)

        def execute(sql, phase, table):
            try:
                db.execute(sql)
                return True
            except sqlite3.Error as e:
                errors.append({"phase": phase, "table": table, "sql": sql,
                               "error": str(e), "sqlite_errorname": getattr(e, "sqlite_errorname", None)})
                return False

        for table in schema["tables"]:
            for sql in [table["ddl"], *table.get("indexes", []), *table.get("examples", [])]:
                if not execute(sql, "schema", table["name"]):
                    break
        # Official generation commits schema/examples before enabling FK enforcement.
        db.commit()
        db.execute("PRAGMA foreign_keys=ON")
        for table in sample["tables"]:
            for sql in table["insert_statements"]:
                execute(sql, "sample", table["table_name"])
        db.commit()
        try:
            fk = [list(r) for r in db.execute("PRAGMA foreign_key_check")]
            integrity = [list(r) for r in db.execute("PRAGMA integrity_check")]
        except sqlite3.Error as e:
            errors.append({"phase": "integrity", "error": str(e),
                           "sqlite_errorname": getattr(e, "sqlite_errorname", None)})
            fk, integrity = [], []
        table_counts = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            table_counts[name] = db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(target) as dest:
                db.backup(dest)
        unknown = any(e.get("sqlite_errorname") in {"SQLITE_AUTH", "SQLITE_INTERRUPT", "SQLITE_NOMEM", "SQLITE_FULL"}
                      or any(s in e["error"].lower() for s in ("no such function", "no such module", "not authorized"))
                      for e in errors)
        return {"errors": errors, "foreign_key_check": fk, "integrity_check": integrity,
                "table_counts": table_counts, "infrastructure_unknown": unknown,
                "complete": not errors and not fk and integrity == [["ok"]]}


def check_environment(db_record, sample_record, env_record, folder):
    compile_error = syntax_error(env_record.get("full_code"))
    try:
        first = initialize(db_record["db_schema"], sample_record["sample_data"], folder / "initial.db")
        attempts = [first]
        if not first["complete"]:
            attempts.append(initialize(db_record["db_schema"], sample_record["sample_data"]))
        stable = len(attempts) == 2 and all(
            canonical({k: a[k] for k in ("errors", "foreign_key_check", "integrity_check")}) ==
            canonical({k: first[k] for k in ("errors", "foreign_key_check", "integrity_check")})
            for a in attempts)
        hard = bool(compile_error or (stable and not any(a["infrastructure_unknown"] for a in attempts)))
        result = {"hard_defect": hard, "initialization_complete": first["complete"],
                  "environment_syntax_error": compile_error, "attempts": attempts,
                  "failure_reproduced": stable, "runtime_tools_checked": False,
                  "scope": "released environment instance; does not prove every task is unsolvable"}
    except (KeyError, TypeError, ValueError, sqlite3.Error) as e:
        result = {"hard_defect": bool(compile_error), "initialization_complete": False,
                  "environment_syntax_error": compile_error, "inspection_error": str(e),
                  "runtime_tools_checked": False}
    write_json(folder / "check.json", result)
    return result


def verifier_binding(matches, task):
    if not matches:
        return {"usable": False, "reason": "missing_verifier"}
    # Deduplicate only byte-equivalent JSON values; never choose conflicting code.
    unique = {canonical(x): x for x in matches}
    if len(unique) != 1:
        return {"usable": False, "reason": "conflicting_verifiers", "records": len(matches)}
    item = next(iter(unique.values()))
    if item.get("task") != task:
        return {"usable": False, "reason": "verifier_task_mismatch"}
    code = item.get("verification", {}).get("code")
    error = syntax_error(code)
    if error:
        return {"usable": False, "reason": "invalid_verifier_code", "details": error}
    tree = ast.parse(code)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "verify_task" for n in tree.body):
        return {"usable": False, "reason": "missing_verify_task_entrypoint"}
    return {"usable": True, "reason": "identical_duplicates_collapsed" if len(matches) > 1 else "unique_binding",
            "records": len(matches), "code_sha256": hashlib.sha256(code.encode()).hexdigest()}


def legacy_metrics(cases):
    eligible = [c for c in cases if c.get("status") == "valid"]
    valid = [c for c in eligible if c.get("modes", {}).get("sql", {}).get("status") == "valid"]
    positive = [c for c in valid if c["oracle"]["correct"]]
    negative = [c for c in valid if not c["oracle"]["correct"]]
    ratio = lambda n, d: {"numerator": n, "denominator": d, "rate": n / d if d else None}
    return {"accuracy": ratio(sum(c["oracle"]["correct"] == c["modes"]["sql"]["accepted"] for c in valid), len(valid)),
            "positive_pass": ratio(sum(c["modes"]["sql"]["accepted"] for c in positive), len(positive)),
            "negative_false_accept": ratio(sum(c["modes"]["sql"]["accepted"] for c in negative), len(negative)),
            "excluded_sql": dict(Counter(c.get("modes", {}).get("sql", {}).get("status", "not_tested") for c in eligible
                                         if c.get("modes", {}).get("sql", {}).get("status") != "valid")),
            "case_statuses": dict(Counter(c.get("status") for c in cases)),
            "label_status": "legacy rule labels, not independently recertified in this run"}


def disposition(environment, asset_issues, binding, risks):
    # Semantic guesses, missing bespoke rules and Judge disagreements cannot reject.
    if environment.get("hard_defect"):
        return "reject", "blocked_environment"
    if asset_issues or not environment.get("initialization_complete"):
        return "retain", "blocked_assets_or_environment_unknown"
    if not binding["usable"]:
        return "retain", "blocked_verifier"
    if risks:
        return "retain", "review_required"
    return "retain", "candidate_unvalidated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/awm-dde80a0")
    parser.add_argument("--legacy", type=Path, default=ROOT / "evidence/legacy")
    parser.add_argument("--scope", choices=["pilot100", "all"], default="pilot100")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-checks", action="store_true", help="Run offline MCP and SQL-Judge program checks (no model calls)")
    parser.add_argument("--probe-cache", type=Path, default=ROOT / "cleaning_runs/two_layer_pilot100_v1")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        parser.error("Output already exists; use a new run directory to preserve evidence")
    manifest = json.loads((args.legacy / "cleaning/manifest.json").read_text())
    if manifest["revision"] != REVISION:
        raise ValueError("Unexpected dataset revision")
    inventory, assets, groups = {}, {}, {}
    for name in FILES:
        path = args.source / name
        digest = sha(path)
        if digest != manifest["files"][name]["sha256"]:
            raise ValueError("Source hash mismatch: " + name)
        assets[name] = rows(path)
        inventory[name] = {"sha256": digest, "records": len(assets[name]), "path": str(path.resolve())}
        grouped = defaultdict(list)
        for i, record in enumerate(assets[name]):
            key = normalize(record.get("scenario", record.get("name", "")))
            if name == "gen_verifier.jsonl":
                key = (key, record.get("task_idx"))
            grouped[key].append((i, record))
        groups[name] = grouped
    tasks = [{"ledger_id": f"row{i}:task{j}", "scenario": r["scenario"], "task_idx": j,
              "task": task, "raw_scenario_row": i}
             for i, r in enumerate(assets["gen_tasks.jsonl"]) for j, task in enumerate(r["tasks"])]
    if len(assets["gen_tasks.jsonl"]) != 1000 or len(tasks) != 10000:
        raise ValueError("Unexpected population size")
    pilot_manifest = json.loads((args.legacy / "audit/manifest.json").read_text())
    if pilot_manifest["revision"] != REVISION:
        raise ValueError("Legacy audit revision mismatch")
    pilot = {(t["scenario"], t["task_idx"]): t["task"] for t in pilot_manifest["tasks"]}
    for key, task in pilot.items():
        matching = [t for t in tasks if (t["scenario"], t["task_idx"]) == key and t["task"] == task]
        if len(matching) != 1:
            raise ValueError("Pilot identity/text mismatch")
    selected = tasks if args.scope == "all" else [t for t in tasks if (t["scenario"], t["task_idx"]) in pilot]
    if args.scope == "pilot100":
        order = {key: i for i, key in enumerate(manifest["pilot100"])}
        selected.sort(key=lambda t: order[t["ledger_id"]])
        if [t["ledger_id"] for t in selected] != manifest["pilot100"]:
            raise ValueError("Original pilot order mismatch")
    legacy_tasks = {f'{t["scenario"]}:{t["task_idx"]}': t for t in rows(args.legacy / "audit/tasks.jsonl")}
    cases = rows(args.legacy / "audit/cases.jsonl")
    by_task = defaultdict(list)
    for i, case in enumerate(cases):
        by_task[case["task"]].append((i, case))
    certificates = {c["ledger_id"]: c for c in rows(args.legacy / "cleaning/task_certificates.jsonl")}
    out.mkdir(parents=True)
    environment_checks, results, queue = {}, [], []
    for t in selected:
        key = f'{t["scenario"]}:{t["task_idx"]}'
        norm = normalize(t["scenario"])
        asset_issues, indices = [], {}
        for name in FILES:
            if name == "gen_verifier.jsonl":
                continue
            matching = groups[name].get(norm, [])
            indices[name] = [i for i, _ in matching]
            if len(matching) != 1:
                asset_issues.append({"file": name, "records": len(matching)})
        if norm not in environment_checks:
            if asset_issues:
                environment_checks[norm] = {"hard_defect": False, "initialization_complete": False, "asset_issues": asset_issues}
            else:
                environment_checks[norm] = check_environment(
                    groups["gen_db.jsonl"][norm][0][1], groups["gen_sample.jsonl"][norm][0][1],
                    groups["gen_envs.jsonl"][norm][0][1], out / "environments" / norm)
            if len(environment_checks) % 25 == 0:
                print(f"Checked {len(environment_checks)} environments", flush=True)
        vmatches = groups["gen_verifier.jsonl"].get((norm, t["task_idx"]), [])
        indices["gen_verifier.jsonl"] = [i for i, _ in vmatches]
        binding = verifier_binding([v for _, v in vmatches], t["task"])
        old = legacy_tasks.get(key, {})
        risks, evidence = [], []
        if old.get("status") in {"confirmed_defect", "needs_review", "runtime_blocked"}:
            risks.append("legacy_" + old["status"])
            evidence.append({"kind": "legacy_task_state", "status": old["status"], "reason": old.get("reason"),
                             "source": str((args.legacy / "audit/tasks.jsonl").resolve())})
        for i, case in by_task[key]:
            sql = case.get("modes", {}).get("sql", {})
            valid = case.get("status") == "valid" and sql.get("status") == "valid"
            if valid and sql["accepted"] != case["oracle"]["correct"]:
                risks.append("legacy_judge_label_disagreement")
                evidence.append({"kind": "legacy_disagreement", "case": case["case"], "line": i + 1,
                                 "oracle_correct": case["oracle"]["correct"], "judge_accepted": sql["accepted"],
                                 "source": str((args.legacy / "audit/cases.jsonl").resolve()),
                                 "original_evaluation": case.get("evaluation_path")})
            if sql.get("status") == "llm_invalid_output":
                risks.append("legacy_invalid_judge_output")
        cert = certificates.get(t["ledger_id"])
        if cert and cert.get("decision") == "quarantine":
            # Preserve prior uncertainty, without turning absent certification into rejection.
            evidence.append({"kind": "legacy_certification", "decision": "quarantine", "reasons": cert.get("reasons", [])})
            if any("disput" in str(x).lower() for x in cert.get("reasons", [])):
                risks.append("legacy_label_dispute")
        risks = sorted(set(risks))
        env = environment_checks[norm]
        decision, reward_status = disposition(env, asset_issues, binding, risks)
        result = {**t, "task_id": key, "decision": decision, "reward_status": reward_status,
                  "semantic_status": "not_certified", "training_ready": False,
                  "risk_flags": risks, "asset_issues": asset_issues, "asset_indices": indices,
                  "verifier_binding": binding, "legacy_evidence": evidence,
                  "environment_evidence": f"environments/{norm}/check.json" if not asset_issues else None,
                  "reject_reason": "reproduced_released_environment_defect" if decision == "reject" else None}
        results.append(result)
        if decision == "retain" and (risks or not binding["usable"] or asset_issues or not env.get("initialization_complete")):
            queue.append({"task_id": key, "ledger_id": t["ledger_id"], "priority": 1 if
                          "legacy_judge_label_disagreement" in risks or not binding["usable"] else 2,
                          "reward_status": reward_status, "risk_flags": risks, "evidence": evidence,
                          "review_action": "Check original task, initial/final DB and actual tool trace; review both the rule label and SQL evidence; do not use Judge majority as truth."})
    for decision in ("retain", "reject"):
        write_rows(out / f"tasks.{decision}.jsonl", [r for r in results if r["decision"] == decision])
    write_rows(out / "tasks.ledger.jsonl", results)
    write_rows(out / "review_queue.jsonl", sorted(queue, key=lambda r: (r["priority"], r["task_id"])))
    write_rows(out / "environment_checks.jsonl", [{"scenario": n, **v} for n, v in environment_checks.items()])
    pilot_results = [r for r in results if (r["scenario"], r["task_idx"]) in pilot]
    summary = {"scope": args.scope, "revision": REVISION, "population_tasks": len(tasks),
               "screened_tasks": len(results), "not_screened_tasks": len(tasks) - len(results),
               "environments": len(environment_checks), "decisions": dict(Counter(r["decision"] for r in results)),
               "pilot100_decisions": dict(Counter(r["decision"] for r in pilot_results)),
               "reward_statuses": dict(Counter(r["reward_status"] for r in results)),
               "binding_statuses": dict(Counter(r["verifier_binding"]["reason"] for r in results)),
               "review_queue_tasks": len(queue), "fresh_model_requests": 0, "semantic_certified_tasks": 0,
               "legacy_sql_metrics": legacy_metrics(cases)}
    write_json(out / "summary.json", summary)
    write_json(out / "manifest.json", {"created_at": datetime.now(timezone.utc).isoformat(),
               "dataset_revision": REVISION, "scope": args.scope, "sources": inventory,
               "script_sha256": sha(Path(__file__)), "python": sys.version, "sqlite": sqlite3.sqlite_version,
               "clock_policy": "recorded wall clock; original SQL dates unchanged",
               "legacy_hashes": {str(p.relative_to(args.legacy)): sha(p) for p in [
                   args.legacy / "audit/manifest.json", args.legacy / "audit/cases.jsonl",
                   args.legacy / "audit/tasks.jsonl", args.legacy / "cleaning/manifest.json",
                   args.legacy / "cleaning/task_certificates.jsonl"]},
               "selected_ledger_ids": [r["ledger_id"] for r in results],
               "policy": "Reject reproduced released environment defects; retain uncertainty with separate reward eligibility; no semantic certification."})
    lines = ["# AWM 缺陷筛除首轮结果", "",
             f"范围：{args.scope}；{len(environment_checks)} 个环境，{len(results)} 道题。数据版本 `{REVISION}`。", "",
             f"保留候选 {summary['decisions'].get('retain', 0)}，排除当前环境实例下的任务 {summary['decisions'].get('reject', 0)}。",
             f"原 100 题：{json.dumps(summary['pilot100_decisions'], ensure_ascii=False)}。", "",
             "保留表示本轮未找到足以排除的环境硬缺陷，不表示答案正确、工具业务行为正确或奖励可靠。所有任务 training_ready=false。",
             "排除仅适用于当前发布环境实例：原始 SQL 无法完整导入，或环境 Python 存在语法错误；不证明每道题在修复后仍不可解。原始数据没有被删除或修改。", "",
             "## 实际检查", "",
             "七类原始资产与旧冻结 manifest 的 SHA-256 比对；逐题绑定、同名冲突；环境和 verifier Python 编译检查；原版 DDL/样本 SQL 真实 SQLite 导入。失败在第二个全新数据库重放，保存逐条 SQL、错误和数据库。",
             "本轮不执行全量 MCP 工具、Agent 解题或新 SQL+LLM 判分。时间相关 SQL 使用本轮真实时钟；记录版本与时间，未声称初态跨日期完全一致。", "",
             "## 判分使用状态", "", "| 状态 | 数量 |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in summary["reward_statuses"].items()]
    lines += ["", "candidate_unvalidated 仅为后续试跑候选；review_required 需优先核查；blocked_verifier 不可直接用于 SQL+LLM 奖励。", "",
              "## 旧报告重算", "",
              "旧规则标签下 SQL+LLM 判对 216/224（96.4%）；正例通过 43/48（89.6%）；负例误放 3/176（1.7%）。",
              "27 个无效模型输出、8 个资产错误未进入 224 的分母。它衡量构造案例判分与旧规则的一致性，不是全库答案正确率；本轮未重新认证旧规则。", "",
              "旧清洗 0 保留的主因之一是只有 1 题完成逐题认证插件。缺少插件不再作为排除条件。旧分歧与失败完整留在风险标签和 review_queue.jsonl，不能把这些候选当作已洗净的训练集。", "",
              f"本轮新模型请求 0；语义认证任务 0；待优先复核 {len(queue)} 题；未筛查 {summary['not_screened_tasks']} 题。", "",
              "## 下一阶段", "",
              "先复核旧 8 个判分分歧（同时允许旧规则错、SQL 取证错、Judge 错和题意歧义），冻结少量明确正例及错对象、漏要求等反例；再对候选中的分层随机样本真实跑工具与 SQL+LLM。",
              "规则只提取题面明确条件，每条附原文依据，不把单一路径、措辞、额外数量限制或内部实现当作用户要求。正常样本一次初筛；仅有矛盾的案例再复核，模型多数票不作为真值。", ""]
    (out / "report_zh.md").write_text("\n".join(lines))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.runtime_checks:
        from zero_llm_screen import extend
        extend(out, results, assets, args.probe_cache.resolve())


if __name__ == "__main__":
    main()
