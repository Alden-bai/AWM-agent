#!/usr/bin/env python3
"""Cross-check all reconstructed DBs using the unmodified official initializer."""
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-world-model"))
from awm.core.db import create_sqlite_database
from awm.core.sample import execute_sample_data
from loguru import logger


def main():
    folder = ROOT / "evidence/official_imports"
    folder.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(str(folder / "official.log"))
    load = lambda name: [json.loads(l) for l in (ROOT / "data/awm-dde80a0" / name).open()]
    schemas = {r["scenario"]: r["db_schema"] for r in load("gen_db.jsonl")}
    samples = {r["scenario"]: r["sample_data"] for r in load("gen_sample.jsonl")}
    checks = [json.loads(l) for l in (ROOT / "cleaning_runs/all_v1/environment_checks.jsonl").open()]
    findings = []
    for check in checks:
        name = check["scenario"]
        path, good_tables, bad_tables, schema_errors = create_sqlite_database(name, schemas[name], str(folder / "databases"))
        good_inserts, bad_inserts, sample_errors = execute_sample_data(path, samples[name], name)
        with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
            fk = db.execute("PRAGMA foreign_key_check").fetchall()
            integrity = db.execute("PRAGMA integrity_check").fetchall()
        complete = bad_tables == 0 and bad_inserts == 0 and not fk and integrity == [("ok",)]
        findings.append({"scenario": name, "official_complete": complete,
                         "screen_complete": check["initialization_complete"],
                         "agrees": complete == check["initialization_complete"],
                         "successful_tables": good_tables, "failed_tables": bad_tables,
                         "successful_inserts": good_inserts, "failed_inserts": bad_inserts,
                         "schema_errors": sorted(schema_errors), "sample_errors": sorted(sample_errors),
                         "foreign_key_check": fk, "integrity_check": integrity})
        if len(findings) % 100 == 0:
            print(f"Official cross-check: {len(findings)} environments", flush=True)
    (folder / "checks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in findings))
    summary = {"environments": len(findings), "agree": sum(r["agrees"] for r in findings),
               "mismatches": [r["scenario"] for r in findings if not r["agrees"]],
               "official_incomplete": sum(not r["official_complete"] for r in findings)}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    if summary["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
