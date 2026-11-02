"""Conservative, persisted stage/input-size windows; no semantic evaluation."""
import collections
import math
import time

from rubric_luna_runtime_v1 import Gate


def p95(values):
    return sorted(values)[math.ceil(.95*len(values))-1]


def summarize(rows, start, end, limit):
    groups = collections.defaultdict(list)
    for row in rows:
        if row.get('success') and row.get('model_seconds') is not None:
            # Adjacent sizes in one stratum differ by at most 50%.
            bucket = math.floor(math.log(max(1, row['input_bytes']), 1.5))
            groups[row['stage']+':'+str(bucket)].append(row['model_seconds'])
    return dict(start=start, end=end, seconds=end-start, limit=limit,
                calls=len(rows), failures=sum(r['infrastructure_failure'] for r in rows),
                rate_limits=sum(bool(r.get('rate_limited')) for r in rows),
                groups={k:dict(n=len(v), p95=p95(v), rate=len(v)/(end-start))
                        for k,v in groups.items()})


def compare(previous, current):
    a,b = previous['groups'],current['groups']
    keys = [k for k in a.keys() & b.keys() if a[k]['n'] >= 5 and b[k]['n'] >= 5]
    matched_a,matched_b = sum(a[k]['n'] for k in keys),sum(b[k]['n'] for k in keys)
    coverage_a = matched_a/max(1,sum(g['n'] for g in a.values()))
    coverage_b = matched_b/max(1,sum(g['n'] for g in b.values()))
    qualified = bool(keys) and min(coverage_a,coverage_b) >= .8
    # Fixed previous-window weights prevent phase mixture from driving decisions.
    ratio = sum(a[k]['n']/max(1,matched_a)*b[k]['rate']/a[k]['rate'] for k in keys)
    latency = sum(a[k]['n']/max(1,matched_a)*b[k]['p95']/a[k]['p95'] for k in keys)
    return dict(qualified=qualified, coverage_previous=coverage_a, coverage_current=coverage_b,
                strata=keys, throughput_ratio=ratio, p95_ratio=latency)


class AdaptiveGate(Gate):
    def __init__(self,persist,state=None,clock=time.time):
        prior = state or {}
        # Only production state contributes timing windows; old trial timings mix queue time.
        scale = prior.get('autoscale') or {}
        self.window_start = scale.get('window_start',clock())
        self.window_rows = scale.get('rows',[])
        self.windows = scale.get('windows',[])
        self.stable = scale.get('stable',{})
        self.bad_windows = scale.get('bad_windows',0)
        self.level_since = scale.get('level_since',clock())
        self.level_calls = scale.get('level_calls',0)
        self.level_failed = scale.get('level_failed',False)
        self.last_decision = scale.get('last_decision','baseline_observation')
        super().__init__(persist, limit=prior.get('limit',3) if scale else 3,
                         max_calls=12000,state=state,clock=clock)
        self.launched = prior.get('calls_launched',0) if scale else 0
        self.peak = max(self.peak,prior.get('peak',0))
        # Restart downtime is never counted as throughput observation.
        if scale:
            self.window_start = clock()
            self.window_rows = []
            self.level_since = clock()
            self.level_calls = 0
            self.last_decision = 'restart_reobserve_level_preserve_cooldown_and_circuit'

    def state(self):
        value = super().state()
        value['autoscale'] = dict(window_start=self.window_start,rows=self.window_rows,
            windows=self.windows[-48:],stable=self.stable,bad_windows=self.bad_windows,
            level_since=self.level_since,level_calls=self.level_calls,
            level_failed=self.level_failed,last_decision=self.last_decision)
        return value

    def reset_level(self):
        self.level_since = self.window_start = self.clock()
        self.window_rows = []
        self.level_calls = self.bad_windows = 0
        self.level_failed = False

    async def complete(self,key,**kwargs):
        detail = dict(kwargs.get('detail') or {})
        detail['rate_limited'] = bool(kwargs.get('rate_limited'))
        # Requests launched before a level change do not qualify the new level.
        detail['launch_limit'] = self.active[key].get('launch_limit',self.limit)
        kwargs['detail'] = detail
        old = self.limit
        await super().complete(key,**kwargs)
        async with self.condition:
            if self.limit != old or kwargs.get('rate_limited'):
                self.reset_level()
                self.last_decision = 'rate_limit_cooldown_reobserve'
            row = self.history[-1]
            if detail['launch_limit'] == self.limit and not kwargs.get('rate_limited'):
                self.window_rows.append(row)
                self.level_calls += 1
                self.level_failed |= bool(kwargs.get('infrastructure_failure'))
            await self.save('autoscale_sample')

    async def launch(self,key,before,spawn):
        proc,cancelled = await super().launch(key,before,spawn)
        # No await: still the same event-loop turn as launch authorization.
        self.active[key]['launch_limit'] = self.limit
        return proc,cancelled

    async def observe(self):
        async with self.condition:
            end = self.clock()
            if self.stop_reason or self.requested_stop or end < self.cooldown_until:
                return
            if end-self.window_start < 300 or len(self.window_rows) < 30:
                return
            window = summarize(self.window_rows,self.window_start,end,self.limit)
            lower = self.stable.get(str(self.limit-3))
            comparison = compare(lower,window) if lower else None
            window['comparison'] = comparison
            self.windows.append(window)
            clean = not window['failures'] and not window['rate_limits']
            changed = False
            if comparison and comparison['qualified'] and comparison['p95_ratio'] > 2 and comparison['throughput_ratio'] <= 1:
                self.bad_windows += 1
            else:
                self.bad_windows = 0
            if self.bad_windows >= 2 and self.limit > 3:
                self.limit -= 3
                self.last_decision = 'two_qualified_slow_windows_downshift'
                changed = True
            elif clean and not self.level_failed and end-self.level_since >= 300 and self.level_calls >= 30:
                if self.limit == 3 or comparison and comparison['qualified'] and comparison['throughput_ratio'] > 1.05:
                    self.stable[str(self.limit)] = window
                    if self.limit < 12:
                        self.limit += 3
                        self.last_decision = 'qualified_promotion'
                        changed = True
                    else:
                        self.last_decision = 'stable_at_hard_limit'
                else:
                    self.last_decision = 'insufficient_comparable_improvement_keep_level'
            else:
                self.last_decision = 'infrastructure_failure_keep_level'
            if changed:
                self.reset_level()
            else:
                self.window_start,self.window_rows = end,[]
                # A fresh clean observation can establish stability after a failure.
                self.level_since,self.level_calls,self.level_failed = end,0,False
            await self.save('autoscale_window')
            self.condition.notify_all()
