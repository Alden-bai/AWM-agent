import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from .io import db_check, db_fingerprint, digest, file_hash, read_json, snapshot, write_json
from .isolation import clean_env, command, stop_process, wait_process

WORKER = Path(__file__).with_name("worker.py").resolve()


async def worker(config, mode, folder, payload):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    request, output = folder / "input.json", folder / "output.json"
    write_json(request, payload)
    argv = [config.runtime_python, str(WORKER), mode, config.official_root, str(request), str(output)]
    argv = command(config, argv, [folder], [WORKER, Path(config.official_root) / "awm"])
    with (folder / "worker.log").open("w") as log:
        process = subprocess.Popen(argv, env=clean_env(config.timezone),
                                   stdout=log, stderr=log, start_new_session=True)
        try:
            await wait_process(process, config.evaluator_timeout)
            if process.returncode != 0:
                raise RuntimeError(f"{mode}_worker_failed: {folder / 'worker.log'}")
            return read_json(output)
        finally:
            await stop_process(process, config.stop_timeout)


class AWMEnvAdapter:
    def __init__(self, task, config, folder):
        self.task, self.config, self.folder = task, config, Path(folder).resolve()
        self.runtime = self.folder / "runtime"
        self.process, self.log = None, None
        self.session_task = None
        self.requests = asyncio.Queue()
        self.tools = []
        self.started = False

    async def prepare(self):
        task, config = self.task, self.config
        deadline = time.monotonic() + config.episode_timeout
        if not task.readiness_profile.get("runtime_ready"):
            raise ValueError("runtime_not_ready")
        if file_hash(task.initial_state_ref) != task.initial_state_hash:
            raise ValueError("initial_hash_mismatch")
        db_check(task.initial_state_ref)
        if db_fingerprint(task.initial_state_ref) != task.initial_state_fingerprint:
            raise ValueError("initial_fingerprint_mismatch")
        env = read_json(task.environment_ref)
        if digest(env) != task.environment_code_hash:
            raise ValueError("environment_hash_mismatch")
        snapshot(task.initial_state_ref, self.folder / "initial.db")
        self.runtime.mkdir()
        snapshot(self.folder / "initial.db", self.runtime / "working.db")
        self.port = self.free_port()
        # Generate official server outside execution sandbox, then expose only generated code + working DB.
        build = self.folder / "server_build"
        build.mkdir()
        envs = build / "envs.jsonl"
        envs.write_text(__import__("json").dumps(env) + "\n")
        await worker(config.model_copy(update={"evaluator_timeout": min(config.evaluator_timeout, config.episode_timeout)}), "generate", build, {"scenario": task.scenario, "envs": str(envs),
                     "db": str(self.runtime / "working.db"), "port": self.port, "script": str(build / "server.py")})
        shutil.copyfile(build / "server.py", self.runtime / "server.py")
        argv = command(config, [config.runtime_python, str(self.runtime / "server.py")], [self.runtime])
        environ = clean_env(config.timezone)
        environ.update(HOST="127.0.0.1", PORT=str(self.port))
        self.log = (self.folder / "server.log").open("w")
        self.process = subprocess.Popen(argv, cwd=self.runtime, env=environ,
                                        stdout=self.log, stderr=self.log, start_new_session=True)
        import httpx
        async with httpx.AsyncClient(timeout=1, trust_env=False) as http:
            async with asyncio.timeout(min(config.startup_timeout, max(.001, deadline - time.monotonic()))):
                while True:
                    if self.process.poll() is not None:
                        raise RuntimeError("environment_exited_during_startup")
                    try:
                        response = await http.get(f"http://127.0.0.1:{self.port}/openapi.json")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.1)
        self.ready = asyncio.get_running_loop().create_future()
        self.session_task = asyncio.create_task(self._session_owner())
        async with asyncio.timeout(min(config.startup_timeout, max(.001, deadline - time.monotonic()))):
            self.tools = await asyncio.shield(self.ready)
        if task.tool_schema_hash is not None and digest(self.tools) != task.tool_schema_hash:
            raise ValueError("tool_schema_hash_mismatch")
        from jsonschema import Draft202012Validator
        if len({t["name"] for t in self.tools}) != len(self.tools):
            raise ValueError("duplicate_tool_names")
        def validate_refs(value):
            if isinstance(value, dict):
                if "$ref" in value and not value["$ref"].startswith("#"):
                    raise ValueError("external_tool_schema_reference_not_allowed")
                for child in value.values():
                    validate_refs(child)
            elif isinstance(value, list):
                for child in value:
                    validate_refs(child)
        for tool in self.tools:
            Draft202012Validator.check_schema(tool["inputSchema"])
            validate_refs(tool["inputSchema"])
        write_json(self.folder / "tool_catalog.json", {"tools": self.tools, "hash": digest(self.tools)})
        self.started = True

    async def _session_owner(self):
        """Keep all AnyIO scopes inside one task; caller timeouts never corrupt them."""
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        pending = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None), trust_env=False) as http:
                async with streamable_http_client(f"http://127.0.0.1:{self.port}/mcp", http_client=http,
                                                   terminate_on_close=False) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        self.ready.set_result([t.model_dump(mode="json") for t in (await client.list_tools()).tools])
                        while True:
                            name, arguments, pending = await self.requests.get()
                            response = (await client.call_tool(name, arguments)).model_dump(mode="json")
                            if not pending.done():
                                pending.set_result(response)
                            pending = None
        except BaseException as exc:
            # Convert task-group cancellation to a transport failure for the caller.
            error = RuntimeError("mcp_session_ended: " + type(exc).__name__)
            if not self.ready.done():
                self.ready.set_exception(error)
            if pending is not None and not pending.done():
                pending.set_exception(error)
            # No exception escapes into an unrelated Actor/controller cancel scope.

    @staticmethod
    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def call(self, name, arguments):
        if self.process.poll() is not None or self.session_task.done():
            raise RuntimeError("environment_exited")
        future = asyncio.get_running_loop().create_future()
        self.requests.put_nowait((name, arguments, future))
        async with asyncio.timeout(self.config.tool_timeout):
            try:
                return await asyncio.shield(future)
            except BaseException:
                future.cancel()
                raise

    def audit(self, label):
        path = self.folder / "audit" / (label + ".db")
        snapshot(self.runtime / "working.db", path)
        return {"path": str(path.relative_to(self.folder)), "sha256": file_hash(path),
                "logical_hash": db_fingerprint(path)}

    async def close(self):
        async def cleanup():
            try:
                if self.session_task:
                    self.session_task.cancel()
                    try:
                        await asyncio.wait_for(self.session_task, self.config.stop_timeout)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
                return await stop_process(self.process, self.config.stop_timeout)
            finally:
                if self.log:
                    self.log.close()
        # A second cancellation may not interrupt process quiescence checks.
        cleanup_task = asyncio.create_task(cleanup())
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            return await asyncio.shield(cleanup_task)

    def freeze(self):
        if not self.started:
            raise RuntimeError("environment_preparation_incomplete")
        path = self.folder / "final.db"
        snapshot(self.runtime / "working.db", path)
        path.chmod(0o444)
        return file_hash(path)
