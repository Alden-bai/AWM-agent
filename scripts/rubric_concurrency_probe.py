"""Bounded diagnostic replay of frozen generation inputs, never production output.

One serial call is followed by a queue with up to twelve distinct request keys.
The first parallel input exactly matches the serial input. All model children
share one semaphore; the deadline includes both phases and kills/drains children.
No registry, historical artifact, or production batch is changed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import time

import rubric_luna_batch02 as io

ROOT = io.ROOT
BASELINE = ROOT / 'runs/rubric_luna_high_100_20261024'


class Probe:
    def __init__(self, directory, seconds=180, workers=12, launcher=None):
        if not 1 <= workers <= 12 or not 0 < seconds <= 180:
            raise ValueError('workers must be 1..12; seconds must be 0..180')
        self.directory = Path(directory)
        self.seconds, self.workers = seconds, workers
        self.sem = asyncio.Semaphore(workers)
        self.launcher = launcher or asyncio.create_subprocess_exec
        self.claims, self.active, self.peak, self.rows = set(), 0, 0, []
        self.deadline = None

    def event(self, kind, **fields):
        with (self.directory / 'timeline.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=kind, at=io.now(), active=self.active,
                                         elapsed_seconds=time.monotonic()-self.started,
                                         **fields), ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())

    async def stop_child(self, proc):
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()

    async def call(self, key, message_path, phase_deadline):
        async with self.sem:
            if time.monotonic() >= phase_deadline:
                return None
            if key in self.claims:
                raise ValueError('duplicate_request_claim:'+key)
            self.claims.add(key)
            folder = self.directory / 'calls' / key
            identity = dict(request_key=key, model=io.MODEL, reasoning_effort=io.EFFORT,
                            message_ref=str(message_path), message_sha256=io.digest(message_path),
                            diagnostic_replay=True, production_eligible=False)
            io.freeze(folder / 'identity.json', identity)
            io.freeze(folder / 'started.json', dict(at=io.now(), identity=identity))
            cmd = ['codex', 'exec', '--skip-git-repo-check', '--ignore-user-config',
                   '-m', io.MODEL, '-c', 'model_reasoning_effort="high"', '-s', 'read-only',
                   '--json', '-o', str(folder / 'response.txt'), '-']
            start, proc, interrupted = time.monotonic(), None, False
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.event('start', request_key=key)
            error = None
            try:
                with (folder/'events.jsonl').open('xb') as out, (folder/'stderr.txt').open('xb') as err:
                    proc = await self.launcher(*cmd, stdin=asyncio.subprocess.PIPE,
                                              stdout=out, stderr=err, cwd=str(ROOT),
                                              start_new_session=True)
                    communication = asyncio.create_task(proc.communicate(message_path.read_bytes()))
                    try:
                        await asyncio.wait_for(asyncio.shield(communication),
                                               max(0.001, phase_deadline-time.monotonic()))
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        interrupted = True
                        await self.stop_child(proc)
                        await communication
            except Exception as exc:
                error = repr(exc)
                if proc is not None:
                    await self.stop_child(proc)
            finally:
                # Slots are released only after the actual child has exited.
                self.active -= 1
                self.event('end', request_key=key, interrupted=interrupted)
            tid, unavailable, done = io.parse_events(folder)
            try:
                usage = io.base.thread_usage(tid) if tid else dict(usage_unknown=True, error='no_thread')
            except Exception as exc:
                usage = dict(thread_id=tid, usage_unknown=True, error=repr(exc))
            io.freeze(folder/'usage.json', usage)
            valid_json = False
            if (folder/'response.txt').exists():
                try:
                    json.loads((folder/'response.txt').read_text())
                    valid_json = True
                except ValueError:
                    pass
            complete = bool(done and proc is not None and proc.returncode == 0 and
                            usage.get('actual_config_verified') and valid_json)
            row = dict(**identity, elapsed_seconds=time.monotonic()-start,
                       thread_id=tid, returncode=proc.returncode if proc else None,
                       completed=complete, turn_completed=done, json_valid=valid_json,
                       interrupted=interrupted, unavailable=unavailable,
                       usage_unknown=usage.get('usage_unknown', True) or not done,
                       usage=usage.get('actual_usage'), config_verified=usage.get('actual_config_verified', False),
                       tool_call_count=usage.get('tool_call_count'), error=error)
            io.freeze(folder/'result.json', row)
            self.rows.append(row)
            print(json.dumps(dict(request_key=key, completed=complete,
                                  seconds=round(row['elapsed_seconds'], 2),
                                  tokens=(row['usage'] or {}).get('total_tokens'),
                                  usage_unknown=row['usage_unknown'])), flush=True)
            return row

    async def run(self, messages):
        self.started = time.monotonic()
        dispatch_deadline = self.started+self.seconds
        self.deadline = dispatch_deadline+60  # Stop dispatch at 3min, then bounded drain.
        baseline = await self.call('serial_00', messages[0], min(dispatch_deadline, self.started+60))
        serial_end = time.monotonic()
        # Model/config failure is a stop condition, never a model fallback.
        if baseline and baseline['completed'] and not baseline['usage_unknown']:
            queue = asyncio.Queue()
            for i, p in enumerate(messages):
                queue.put_nowait((i, p))
            stop = asyncio.Event()
            failures = 0
            async def worker():
                nonlocal failures
                while not queue.empty() and not stop.is_set() and time.monotonic() < dispatch_deadline:
                    i, path = queue.get_nowait()
                    row = await self.call(f'parallel_{i:02}', path, self.deadline)
                    queue.task_done()
                    if row:
                        failures = 0 if row['completed'] else failures+1
                        if row['unavailable'] or not row['config_verified'] or failures >= 3:
                            stop.set()
                            self.event('dispatch_stopped', reason='configuration_or_infrastructure_failure')
            await asyncio.gather(*(worker() for _ in range(self.workers)))
        else:
            self.event('dispatch_stopped', reason='serial_preflight_not_completed_or_usage_unknown')
        result = dict(seconds_limit=self.seconds, actual_wall_seconds=time.monotonic()-self.started,
                      serial_wall_seconds=serial_end-self.started,
                      parallel_wall_seconds=time.monotonic()-serial_end,
                      drain_limit_seconds=60, target_parallelism=self.workers, peak_active_children=self.peak,
                      remaining_children=self.active, calls=self.rows,
                      known_total_tokens=sum((r['usage'] or {}).get('total_tokens', 0)
                                             for r in self.rows if not r['usage_unknown']),
                      unknown_usage_calls=sum(r['usage_unknown'] for r in self.rows),
                      production_batch_started=False, frozen_history_unchanged=True)
        io.snapshot(self.directory/'summary.json', result)
        return result


def prepare(directory, workers):
    """Replay historical inputs byte-for-byte, without loading historical answers."""
    if (directory/'manifest.json').exists():
        frozen = io.read(directory/'manifest.json')
        assert frozen['workers'] == workers and frozen['max_model_calls'] == workers+1
        assert (frozen['model'], frozen['reasoning_effort']) == (io.MODEL, io.EFFORT)
        paths = sorted((directory/'messages').glob('*.txt'))
        assert len(paths) == workers == len(frozen['inputs'])
        for path, item in zip(paths, frozen['inputs']):
            assert io.digest(path) == item['message_sha256']
            assert io.digest(item['source_ref']) == item['source_sha256']
        return paths
    manifest = io.read(BASELINE/'manifest.json')
    for name in ('generate.txt', 'execute.txt'):
        assert io.digest(BASELINE/'prompts'/name) == manifest['prompt_hashes'][name]
    messages, inputs = [], []
    for t in manifest['tasks']:
        source = BASELINE/'tasks'/t['task_id'].replace(':', '__')/'round_0/generate/input.json'
        if not source.exists():
            continue
        pack = io.read(source)
        assert pack['protocol'] == (BASELINE/'prompts/generate.txt').read_text()
        assert pack['execution'] == (BASELINE/'prompts/execute.txt').read_text()
        path = directory/'messages'/f'{len(messages):02}.txt'
        message = ('按以下完整输入包的execution和protocol处理单题单阶段。使用消息模式，只返回业务JSON，不调用工具，不读取文件，不启动子agent。\n'
                   +json.dumps(pack, ensure_ascii=False, separators=(',', ':')))
        io.freeze(path, message, raw=True)
        inputs.append(dict(task_id=t['task_id'], source_ref=str(source),
                           source_sha256=io.digest(source), message_sha256=io.digest(path)))
        messages.append(path)
        if len(messages) == workers:
            break
    assert len(messages) == workers
    io.freeze(directory/'manifest.json', dict(model=io.MODEL, reasoning_effort=io.EFFORT,
              created_at=io.now(), workers=workers, duration_seconds=180, inputs=inputs,
              implementation_sha256=io.digest(__file__), purpose='concurrency diagnostic replay only',
              registry_modified=False, production_eligible=False, max_model_calls=workers+1,
              matched_pair=['serial_00','parallel_00'],
              startup_prompt_ref=str(ROOT/'configs/rubric/luna_high_3000/run_3000.txt'),
              startup_prompt_sha256=io.digest(ROOT/'configs/rubric/luna_high_3000/run_3000.txt')))
    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 13), default=12)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    with io.locked(directory/'.runner.lock', blocking=False):
        if (directory/'timeline.jsonl').exists():
            raise ValueError('probe_already_started_no_automatic_replay')
        messages = prepare(directory, args.workers)
        if not args.prepare_only:
            io.freeze(directory/'execution.json', dict(manifest_sha256=io.digest(directory/'manifest.json'),
                      implementation_sha256=io.digest(__file__),
                      preparation_fix='Reuse frozen created_at and inputs after prepare-only; no model calls preceded this fix'))
            result = asyncio.run(Probe(directory, workers=args.workers).run(messages))
            print(json.dumps({k:v for k,v in result.items() if k != 'calls'}), flush=True)


if __name__ == '__main__':
    main()
