from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TenantQuota:
    tenant: str
    gpu_compute_share: float = 1.0
    gpu_mem_gb: float = 8.0
    token_bucket: float = 0.0


class QuotaManager:
    def __init__(self, total_gpu_mem_gb: float) -> None:
        self.total_gpu_mem_gb = total_gpu_mem_gb
        self.tenants: dict[str, TenantQuota] = {}
        self.used: dict[str, float] = {}

    def register(self, q: TenantQuota) -> None:
        self.tenants[q.tenant] = q
        self.used.setdefault(q.tenant, 0.0)

    def alloc_mem(self, tenant: str, gb: float) -> bool:
        q = self.tenants.get(tenant)
        if q is None:
            q = TenantQuota(tenant=tenant)
            self.register(q)
        if self.used[tenant] + gb > q.gpu_mem_gb:
            return False
        if sum(self.used.values()) + gb > self.total_gpu_mem_gb:
            return False
        self.used[tenant] += gb
        return True

    def free_mem(self, tenant: str, gb: float) -> None:
        self.used[tenant] = max(0.0, self.used.get(tenant, 0.0) - gb)

    def compute_slice(self, tenant: str, tick: float) -> float:
        q = self.tenants.get(tenant)
        if q is None:
            return tick
        total_share = sum(t.gpu_compute_share for t in self.tenants.values()) or 1.0
        return tick * (q.gpu_compute_share / total_share)

    def allow_slice(self, tenant: str, tick: float) -> bool:
        """Per-decode-slice compute admission (cgroups-like share)."""
        if tenant in self.tenants and self.tenants[tenant].gpu_compute_share <= 0:
            return False
        return self.compute_slice(tenant, tick) > 0

    def is_isolated(self, tenant: str) -> bool:
        q = self.tenants.get(tenant)
        if q is None:
            return True
        return self.used.get(tenant, 0.0) <= q.gpu_mem_gb + 1e-9
