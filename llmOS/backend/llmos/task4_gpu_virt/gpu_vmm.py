from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from llmos.errors import IsolationError


@dataclass
class GpuPage:
    tenant: str
    vpn: int
    ppn: Optional[int] = None
    resident: bool = False
    dirty: bool = False
    last_access: float = 0.0


class GPUVMM:
    def __init__(self, phys_pages: int, page_size_gb: float = 0.25) -> None:
        self.phys_pages = phys_pages
        self.page_size_gb = page_size_gb
        self.free_ppn = list(range(phys_pages))
        self.tables: Dict[str, Dict[int, GpuPage]] = {}
        self.vmem_pages: Dict[str, int] = {}
        self.violations_attempt = 0
        self.violations_unblocked = 0
        self.page_faults = 0
        self.swap_outs = 0

    def create_tenant(self, tenant: str, vmem_pages: int) -> None:
        self.tables[tenant] = {
            vpn: GpuPage(tenant=tenant, vpn=vpn) for vpn in range(vmem_pages)
        }
        self.vmem_pages[tenant] = vmem_pages

    def access(self, tenant: str, vpn: int, write: bool, now: float) -> int:
        table = self.tables.get(tenant)
        if table is None or vpn not in table:
            self.violations_attempt += 1
            raise IsolationError(f"{tenant} vpn={vpn}")
        page = table[vpn]
        if not page.resident:
            self.page_faults += 1
            if not self.free_ppn:
                self._evict(now)
            if not self.free_ppn:
                raise IsolationError("no physical pages")
            page.ppn = self.free_ppn.pop()
            page.resident = True
        page.last_access = now
        if write:
            page.dirty = True
        return int(page.ppn)

    def _evict(self, now: float) -> None:
        residents = [
            p
            for table in self.tables.values()
            for p in table.values()
            if p.resident
        ]
        if not residents:
            return
        victim = min(residents, key=lambda p: p.last_access)
        if victim.ppn is not None:
            self.free_ppn.append(victim.ppn)
        victim.ppn = None
        victim.resident = False
        victim.dirty = False
        self.swap_outs += 1

    def balloon_reclaim(self, tenant: str, pages: int) -> int:
        table = self.tables.get(tenant, {})
        reclaimed = 0
        for p in sorted(table.values(), key=lambda x: x.last_access):
            if reclaimed >= pages:
                break
            if p.resident and p.ppn is not None:
                self.free_ppn.append(p.ppn)
                p.ppn = None
                p.resident = False
                reclaimed += 1
        return reclaimed

    def oversubscription_ratio(self) -> float:
        logical = sum(self.vmem_pages.values())
        return logical / max(1, self.phys_pages)

    def isolation_violation_rate(self) -> float:
        if self.violations_attempt == 0:
            return 0.0
        return self.violations_unblocked / self.violations_attempt
