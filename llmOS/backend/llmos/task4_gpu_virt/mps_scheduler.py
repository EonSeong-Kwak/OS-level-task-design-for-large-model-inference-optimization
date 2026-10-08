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

    def register(self, t: TenantCompute) -> None:
        self.tenants[t.tenant] = t

    def pick_next(self, now: float) -> str:
        if not self.tenants:
            return "default"
        # weighted deficit: pick tenant with least accumulated / share
        def key(item: tuple[str, TenantCompute]) -> float:
            name, t = item
            return t.accumulated / max(t.share, 1e-9)

        name, _ = min(self.tenants.items(), key=key)
        return name

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
