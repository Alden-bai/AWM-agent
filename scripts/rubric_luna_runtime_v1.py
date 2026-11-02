"""Production dispatch lifecycle; business rules remain in frozen batch03."""
import asyncio
from collections import deque
import random
import time


from rubric_luna_dispatch import DispatchPaused, Writer, finish_cleanup


class Gate:
    def __init__(self, persist, limit=3, max_calls=13, state=None, clock=time.time):
        if not 1 <= limit <= 12 or not 1 <= max_calls:
            raise ValueError('unsafe_production_configuration')
        self.persist, self.clock = persist, clock
        self.condition = asyncio.Condition()
        self.limit, self.max_calls = limit, max_calls
        self.active, self.remote_holds, self.claims = {}, {}, set()
        self.launched, self.peak = 0, 0
        self.stop_reason = None
        self.requested_stop = None
        self.recent = deque(maxlen=20)
        self.consecutive = 0
        self.cooldown_until = 0
        self.backoff_attempt = 0
        self.stopped = asyncio.Event()
        self.history = []
        if state:
            self.recent.extend(state.get('recent_infrastructure_failures', []))
            self.consecutive = state.get('consecutive_infrastructure_failures', 0)
            self.cooldown_until = state.get('cooldown_until', 0)
            self.backoff_attempt = state.get('backoff_attempt', 0)
            self.remote_holds.update(state.get('remote_holds', {}))
            self.remote_holds.update(state.get('active', {}))
            self.history = state.get('stage_completions', [])
            # Persisted unresolved requests never become free slots on restart.
            if state.get('active') or self.remote_holds:
                self.stop_reason = 'unresolved_requests_require_reconciliation'
            elif state.get('stop_reason') in ('infrastructure_circuit_open', 'model_configuration', 'writer_failure'):
                self.stop_reason = state['stop_reason']
        if self.stop_reason:
            self.stopped.set()

    def state(self):
        return dict(limit=self.limit, initial_concurrency=3, target_concurrency=12,
                    max_concurrency=12, worker_capacity=12, active=self.active.copy(),
                    remote_holds=self.remote_holds.copy(), calls_launched=self.launched,
                    peak=self.peak, stop_reason=self.stop_reason,
                    recent_infrastructure_failures=list(self.recent),
                    consecutive_infrastructure_failures=self.consecutive,
                    cooldown_until=self.cooldown_until, backoff_attempt=self.backoff_attempt,
                    stage_completions=self.history[-120:])

    async def save(self, event):
        try:
            await self.persist(event, self.state())
        except Exception:
            self.stop_reason = 'writer_failure'
            self.requested_stop = self.stop_reason
            self.stopped.set()
            self.condition.notify_all()
            raise

    async def stop(self, reason):
        self.requested_stop = reason
        async with self.condition:
            self.stop_reason = self.stop_reason or reason
            self.stopped.set()
            self.condition.notify_all()
            await self.save('dispatch_stopped')

    async def set_limit(self, limit):
        if not 1 <= limit <= 12:
            raise ValueError('global_limit_out_of_range')
        async with self.condition:
            self.limit = limit
            # Existing permits are retained; lowering a limit cancels no call.
            await self.save('concurrency_changed')
            self.condition.notify_all()

    async def launch(self, key, before, spawn):
        """Stop, limit check, durable intent and launch authorization are serialized."""
        async with self.condition:
            while True:
                if self.requested_stop or self.stop_reason:
                    raise DispatchPaused(self.stop_reason or self.requested_stop)
                if self.launched >= self.max_calls:
                    self.stop_reason = 'authorized_call_limit'
                    self.stopped.set()
                    self.condition.notify_all()
                    await self.save('authorized_call_limit')
                    raise DispatchPaused(self.stop_reason)
                if self.cooldown_until > self.clock():
                    try:
                        await asyncio.wait_for(self.condition.wait(), min(1, self.cooldown_until-self.clock()))
                    except asyncio.TimeoutError:
                        pass
                    continue
                if len(self.active)+len(self.remote_holds) < self.limit:
                    break
                await self.condition.wait()
            if key in self.claims:
                raise ValueError('duplicate_request_claim:'+key)
            self.claims.add(key)
            self.active[key] = dict(launch_authorized_at=self.clock(), phase='intent')
            try:
                await before()
                # A stop requested while durable writes were pending wins.
                if self.requested_stop:
                    raise DispatchPaused(self.requested_stop)
                await self.save('launch_intent')
                if self.requested_stop:
                    raise DispatchPaused(self.requested_stop)
            except BaseException as exc:
                self.active.pop(key)
                if not isinstance(exc,(DispatchPaused,asyncio.CancelledError)):
                    self.stop_reason = 'writer_failure'
                    self.stopped.set()
                self.condition.notify_all()
                raise
            spawning = asyncio.create_task(spawn())
            cancelled = False
            try:
                proc = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                cancelled = True
                proc = await finish_cleanup(spawning)
            except BaseException:
                self.active.pop(key)
                self.condition.notify_all()
                raise
            self.launched += 1
            self.peak = max(self.peak, len(self.active))
            self.active[key].update(pid=proc.pid, phase='spawned', spawned_at=self.clock())
            # Caller owns proc even if the next durable write fails or it was cancelled.
            return proc, cancelled

    async def spawned(self, key, identity):
        async with self.condition:
            self.active[key].update(identity)
            await self.save('process_started')

    async def complete(self, key, *, infrastructure_failure=False, model_bad=False,
                       unresolved=False, rate_limited=False, retry_after=None, detail=None):
        async with self.condition:
            active = self.active.pop(key)
            if unresolved:
                self.remote_holds[key] = active
                self.stop_reason = self.stop_reason or 'unresolved_remote_request'
            self.recent.append(bool(infrastructure_failure))
            self.consecutive = self.consecutive+1 if infrastructure_failure else 0
            self.history.append(dict(completed_at=self.clock(), key=key,
                                     infrastructure_failure=infrastructure_failure, **(detail or {})))
            if model_bad:
                self.stop_reason = 'model_configuration'
            elif self.consecutive >= 3 or sum(self.recent) >= 5:
                self.stop_reason = 'infrastructure_circuit_open'
            if rate_limited:
                self.limit = max(3, self.limit-3)
                self.backoff_attempt += 1
                base = min(300, 30*2**min(self.backoff_attempt-1, 4))
                delay = max(retry_after or 0, base*random.uniform(1, 1.2))
                self.cooldown_until = max(self.cooldown_until, self.clock()+delay)
            if self.stop_reason:
                self.stopped.set()
            await self.save('stage_completed')
            self.condition.notify_all()
