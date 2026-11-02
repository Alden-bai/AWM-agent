import argparse
import asyncio
import json
from pathlib import Path
from .contracts import RunConfig
from .export import export_sft
from .io import read_json
from .registry import prepare
from .stages import diagnose, reevaluate, run_stage


def main():
    from dotenv import load_dotenv
    from .contracts import ROOT
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="AWM Harness P0-P3; no paid requests by default")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="verify cleaning inputs and rebuild frozen initial states")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--splits", help="JSON mapping scenario to train/dev/test")
    p.add_argument("--admissions", help="JSON mapping task_instance_id to explicit reward admission")
    for name in ("collect", "evaluate"):
        p = sub.add_parser(name)
        p.add_argument("--registry", required=True)
        p.add_argument("--run", required=True)
        p.add_argument("--config", required=True)
        p.add_argument("--scripts", help="JSON mapping scenario:task_id to scripted actions")
        p.add_argument("--task", action="append", dest="task_ids")
    for name in ("reevaluate", "diagnose"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
    p = sub.add_parser("export-sft")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--tokenizer")
    p.add_argument("--chat-template")
    args = parser.parse_args()
    if hasattr(args, "config"):
        config = RunConfig.model_validate(read_json(args.config))
        config = config.model_copy(update={"official_root": str(Path(config.official_root).resolve()),
                                           "runtime_python": str(Path(config.runtime_python).absolute())})
    if args.command == "prepare":
        result = asyncio.run(prepare(args.source, args.output, config, args.splits, args.admissions))
    elif args.command in {"collect", "evaluate"}:
        result = asyncio.run(run_stage(args.command, args.registry, args.run, config, args.scripts, args.task_ids))
    elif args.command == "reevaluate":
        result = asyncio.run(reevaluate(args.run))
    elif args.command == "diagnose":
        result = diagnose(args.run)
    else:
        result = export_sft(args.run, args.output, args.tokenizer, args.chat_template)
    print(json.dumps(result, ensure_ascii=False, indent=2))

