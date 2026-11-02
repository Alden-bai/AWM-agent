#!/usr/bin/env python3
"""Start the cloned official MCP adapter; discover tools and make one read call."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-world-model"))


async def main():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from awm.core.server import Config, _generate_server_code

    folder = ROOT / "evidence/runtime_smoke"
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "working.db"
    shutil.copy2(ROOT / "cleaning_runs/pilot100_v1/environments/inventory_management_6/initial.db", db)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Config(scenario="inventory_management_6", envs_load_path=str(ROOT / "data/awm-dde80a0/gen_envs.jsonl"),
                    db_path=str(db), port=port, temp_server_path=str(folder / "server.py"))
    script = _generate_server_code(config, str(db))
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
    env.update(HOST="127.0.0.1", PORT=str(port), DATABASE_PATH=f"sqlite:///{db}")
    with (folder / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, script], cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as http:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError("Server exited; inspect server.log")
                    try:
                        if (await http.get(f"http://127.0.0.1:{port}/openapi.json")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.25)
                else:
                    raise TimeoutError("Server did not start")
            async with AsyncExitStack() as stack:
                read, write, _ = await stack.enter_async_context(streamablehttp_client(f"http://127.0.0.1:{port}/mcp"))
                client = await stack.enter_async_context(ClientSession(read, write))
                await client.initialize()
                tools = (await client.list_tools()).tools
                (folder / "tools.json").write_text(json.dumps([t.model_dump(mode="json") for t in tools], ensure_ascii=False, indent=2))
                matches = [t for t in tools if t.name == "get_vehicle_by_stock_id" or t.name.startswith("get_vehicle_by_stock_id_")]
                if len(matches) != 1:
                    raise RuntimeError("Expected one stock-id lookup tool")
                result = await client.call_tool(matches[0].name, {"stock_id": "FORD-F150-987"})
                payload = result.model_dump(mode="json")
                passed = not result.isError and "FORD-F150-987" in json.dumps(payload)
                summary = {"passed": passed, "python": sys.executable, "scenario": "inventory_management_6",
                           "tool_count": len(tools), "read_tool": matches[0].name, "arguments": {"stock_id": "FORD-F150-987"},
                           "result": payload, "limits": "one environment startup/discovery/read smoke test; no full business coverage"}
                (folder / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({k: summary[k] for k in ("passed", "scenario", "tool_count", "read_tool")}))
                if not passed:
                    raise RuntimeError("Read smoke test failed")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    asyncio.run(main())
