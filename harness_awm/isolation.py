"""Linux filesystem isolation. Networking is loopback/shared, not a network sandbox."""
import asyncio
import os
import signal
import subprocess
from pathlib import Path


def runtime_info(python):
    import json
    output = subprocess.check_output([python, "-c", "import json,sys; print(json.dumps([sys.prefix,sys.base_prefix]))"], text=True)
    return [Path(p).resolve() for p in json.loads(output)]


def command(config, argv, writable, readonly=()):
    if config.isolation == "development":
        return argv
    # Never bind /, /home, the repository root, run root or credentials directory.
    roots = [Path("/usr"), Path("/lib"), Path("/lib64"), *runtime_info(config.runtime_python)]
    # uv venv interpreters can point through an additional version-alias directory.
    interpreter = Path(config.runtime_python)
    if interpreter.is_symlink():
        link = Path(os.readlink(interpreter))
        if not link.is_absolute():
            link = interpreter.parent / link
        roots.append(link.parent.parent.absolute())
    roots += [Path(p).absolute() for p in readonly]
    cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in dict.fromkeys(roots):
        if path.exists():
            cmd += ["--ro-bind", str(path), str(path)]
    for path in writable:
        path = Path(path).resolve()
        cmd += ["--bind", str(path), str(path)]
    cmd += ["--chdir", str(Path(writable[0]).resolve()), "--", *argv]
    return cmd


def clean_env(timezone):
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": timezone,
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}


async def wait_process(process, timeout):
    # Popen.poll uses waitpid; works on hosts where asyncio pidfd notifications are restricted.
    async with asyncio.timeout(timeout):
        while process.poll() is None:
            await asyncio.sleep(.05)
    return process.returncode


async def stop_process(process, timeout=5):
    if process is None:
        return True
    # Kill the entire session group even if its original leader already exited.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            await wait_process(process, timeout)
        except asyncio.TimeoutError:
            continue
        # bwrap's PID namespace destroys descendants when its init exits.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
    return False

