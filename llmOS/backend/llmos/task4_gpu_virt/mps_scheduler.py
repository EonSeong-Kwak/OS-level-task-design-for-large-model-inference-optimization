from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TenantCompute:
    tenant: str
    share: float = 1.0
    ctx_switch_cost: float = 0.0005
    accumulated: float = 0.0
    last_run: float = -1.0


class MPSScheduler:
    def __init__(self, time_slice: float = 0.01) -> None:
        self.time_slice = time_slice
        self.tenants: dict[str, TenantCompute] = {}
        self._last: str | None = None
        self.switch_count = 0
        # Window ownership: an MPS tenant keeps the GPU for a full time slice
        # instead of being re-elected on every decode tick.
        self.window_owner: str | None = None
        self.window_start: float = -1.0

    def register(self, t: TenantCompute) -> None:
        self.tenants[t.tenant] = t

    def _deficit(self, name: str) -> float:
        t = self.tenants[name]
        return t.accumulated / max(t.share, 1e-9)

    def pick_next(self, now: float) -> str:
        if not self.tenants:
            return "default"
        # weighted deficit: pick tenant with least accumulated / share
        return min(self.tenants.keys(), key=lambda n: self._deficit(n))

    def select_window(self, present: set[str], now: float) -> str:
        """Elect the owner for the current time-slice window.

        The incumbent keeps the GPU until its slice elapses; then the present
        tenant with the smallest accumulated/share deficit is elected.
        """
        candidates = [n for n in present if n in self.tenants] or list(present)
        if not candidates:
            return "default"
        if (
            self.window_owner in candidates
            and self.window_start >= 0
            and now - self.window_start < self.time_slice
        ):
            return self.window_owner
        choice = min(candidates, key=lambda n: self._deficit(n) if n in self.tenants else 0.0)
        self.window_owner = choice
        self.window_start = now
        return choice

    def run_slice(self, tenant: str, now: float) -> float:
        t = self.tenants.get(tenant)
        extra = 0.0
        if t is None:
            t = TenantCompute(tenant=tenant)
            self.register(t)
        if self._last is not None and self._last != tenant:
            extra = t.ctx_switch_cost
            self.switch_count += 1
        self._last = tenant
        served = self.time_slice
        t.accumulated += served
        t.last_run = now
        return extra

    def fairness_index(self) -> float:
        xs = []
        for t in self.tenants.values():
            xs.append(t.accumulated / max(t.share, 1e-9))
        n = len(xs)
        if n == 0:
            return 1.0
        s = sum(xs)
        sq = sum(x * x for x in xs)
        if sq == 0:
            return 1.0
        return (s * s) / (n * sq)
