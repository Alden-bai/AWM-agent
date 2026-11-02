"""Isolated official adapter worker. Executed as a file, not imported by Actor."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


async def main():
    mode, root, request_path, output_path = sys.argv[1:]
    sys.path.insert(0, root)
    data = json.loads(Path(request_path).read_text())
    if mode == "generate":
        from awm.core.server import Config, _generate_server_code
        config = Config(scenario=data["scenario"], envs_load_path=data["envs"], db_path=data["db"],
                        host="127.0.0.1", port=data["port"], temp_server_path=data["script"])
        _generate_server_code(config, data["db"])
        result = {"generated": True}
    elif mode == "initialize":
        from awm.core.db import create_sqlite_database
        from awm.core.sample import execute_sample_data
        path, good_tables, bad_tables, schema_errors = create_sqlite_database(
            data["scenario"], data["schema"], str(Path(output_path).parent))
        good_inserts, bad_inserts, sample_errors = execute_sample_data(path, data["sample"], data["scenario"])
        result = {"path": path, "complete": bad_tables == 0 and bad_inserts == 0,
                  "good_tables": good_tables, "bad_tables": bad_tables,
                  "good_inserts": good_inserts, "bad_inserts": bad_inserts,
                  "schema_errors": list(schema_errors), "sample_errors": list(sample_errors)}
    elif mode == "sql":
        from awm.core.verify import run_verifier
        intermediate, details = run_verifier(data["verifier"], "sql", data["initial"], data["final"])
        result = {"intermediate_reward_type": intermediate, "verify_result": details}
    elif mode == "judge":
        import awm.core.verify as official
        captured = {}

        class RecordingClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def create(self, **kwargs):
                captured.update(kwargs)
                # Transport substitution only: the official function still constructs and parses.
                response = data["response"]
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=response["choices"][0]["message"].get("content")),
                    finish_reason=response["choices"][0].get("finish_reason"))])

        official.AsyncOpenAI = RecordingClient
        classification, details = await official.run_llm_judge(
            data["task"], data["sql"], data["base_url"], "TRANSPORT_REPLACED", data["model"], data["trajectory"])
        result = {"request": captured, "classification": classification, "details": details}
    else:
        raise ValueError("unknown worker mode")
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    asyncio.run(main())

