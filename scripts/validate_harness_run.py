#!/usr/bin/env python3
"""Independent finite DB checks for the four scripted development smoke tasks.

No model requests. Does not certify general task semantics or training reward.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness_awm.export import export_sft
from harness_awm.io import file_hash, read_json, rows, write_json
from harness_awm.stages import diagnose, reevaluate


CHECKS = {
    "it_service_management_itsm_1": (
        "SELECT count(*) FROM incidents WHERE category='network' AND impact_level='high' "
        "AND short_description=? AND lifecycle_status='logged'",
        ["All users on the 3rd floor at London Office cannot connect to the corporate Wi-Fi SSID FIELD-FIX since 09:10 AM GMT"], "new_one"),
    "social_networking_8": (
        "SELECT count(*) FROM posts WHERE author_id=1 AND content=? AND audience='Friends'",
        ["Excited to start 2025 by learning web automation and building my first side project! #NewYear #Coding"], "new_one"),
    "education_training_management_1": (
        "SELECT count(*) FROM competencies WHERE id=340 AND level='intermediate' AND category='pipefitting' AND short_description=?",
        ["Install and test residential copper and PEX water lines"], "updated_one"),
    "e_commerce_10": (
        "SELECT count(*) FROM reviews r JOIN sku s ON s.id=r.sku_id WHERE r.user_id=1 "
        "AND s.sku_code='WHEY-ISOLATE-CHOC-2LB' AND r.rating=5 AND r.status='pending' AND r.review_text=?",
        ["Great taste, mixes easily, and no stomach issues even with my lactose sensitivity"], "new_one"),
}


async def validate(run, output):
    run = Path(run).resolve()
    episodes = sorted((run / "episodes").glob("*/a0"))
    before = {str(p): file_hash(p) for ep in episodes for p in
              [ep / "events.jsonl", ep / "trajectory.json", ep / "initial.db", ep / "final.db"]}
    evaluations_before = {str(ep): len(list((ep / "evaluations").glob("*/attempt_*"))) for ep in episodes}
    metrics = await reevaluate(run)
    after = {name: file_hash(name) for name in before}
    evaluations_after = {str(ep): len(list((ep / "evaluations").glob("*/attempt_*"))) for ep in episodes}
    findings = diagnose(run)
    checks = []
    for ep in episodes:
        task, result = read_json(ep / "task.json"), read_json(ep / "episode_result.json")
        sql, params, kind = CHECKS[task["scenario"]]
        values = []
        for name in ("initial.db", "final.db"):
            with sqlite3.connect((ep / name).as_uri() + "?mode=ro", uri=True) as db:
                values.append(db.execute(sql, params).fetchone()[0])
        changes = [e for e in rows(ep / "events.jsonl") if e["event_type"] == "tool_completed"]
        passed = (values[1] == values[0] + 1 if kind == "new_one" else values == [0, 1])
        checks.append({"scenario": task["scenario"], "task_id": task["native_task_id"], "sql": sql,
                       "parameters": params, "initial_matches": values[0], "final_matches": values[1],
                       "finite_db_check_passed": passed, "termination": result["termination_reason"],
                       "calls": len(changes), "mcp_errors": sum(e["mcp_is_error"] for e in changes),
                       "writes_observed": sum(e["before_state_ref"]["logical_hash"] != e["after_state_ref"]["logical_hash"] for e in changes)})
    export = run / "exports" / "validation"
    if not export.exists():
        export_sft(run, export)
    report = {"run": str(run), "task_checks": checks,
              "reevaluation_execution_unchanged": before == after,
              "reevaluation_reused_valid_results": evaluations_before == evaluations_after,
              "diagnostic_issues": [issue for ep in findings["episodes"] for issue in ep["issues"]],
              "metrics": metrics, "sft_export": read_json(export / "report.json"),
              "limitations": "Scripted Actor and mock Judge; finite DB checks only; no real LLM accuracy or training validation."}
    report["passed"] = (len(checks) == 4 and all(x["finite_db_check_passed"] and x["termination"] == "finish" and x["mcp_errors"] == 0 for x in checks)
                        and before == after and evaluations_before == evaluations_after and not report["diagnostic_issues"]
                        and metrics["budget"]["physical_requests"] == 0 and report["sft_export"]["accepted_episodes"] == 0)
    write_json(output, report)
    print(json.dumps({"passed": report["passed"], "tasks": len(checks), "report": str(Path(output).resolve())}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    asyncio.run(validate(args.run, args.output))
