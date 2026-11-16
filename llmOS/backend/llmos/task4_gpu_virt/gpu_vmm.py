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
    pinned: bool = False  # held by the KV allocator; never swapped out while pinned
    last_access: float = 0.0


class GPUVMM:
    def __init__(
        self,
        phys_pages: int,
        page_size_gb: float = 0.25,
        swap_write_per_page_s: Optional[float] = None,
    ) -> None:
        self.phys_pages = phys_pages
        self.page_size_gb = page_size_gb
        # Dirty pages pay a host swap-write cost on eviction (spec 4.4.1).
        # Default: write one page back over NVMe-class bandwidth (3.5 GB/s).
        self.swap_write_per_page_s = (
            swap_write_per_page_s
            if swap_write_per_page_s is not None
            else page_size_gb / 3.5
        )
        self.free_ppn = list(range(phys_pages))
        self.tables: Dict[str, Dict[int, GpuPage]] = {}
        self.vmem_pages: Dict[str, int] = {}
        self.violations_attempt = 0
        self.violations_unblocked = 0
        self.page_faults = 0
        self.swap_outs = 0
        self.swap_write_delay = 0.0

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

    def pin(self, tenant: str, vpn: int, now: float = 0.0) -> int:
        """Map (if needed) and pin a page currently owned by the KV allocator."""
        ppn = self.access(tenant, vpn, True, now)
        self.tables[tenant][vpn].pinned = True
        return ppn

    def unpin(self, tenant: str, vpn: int) -> None:
        """Release the allocator hold; the frame becomes swappable again."""
        page = self.tables.get(tenant, {}).get(vpn)
        if page is not None:
            page.pinned = False

    def release(self, tenant: str, vpn: int) -> None:
        """Allocator freed/swapped the block: give the physical frame back."""
        page = self.tables.get(tenant, {}).get(vpn)
        if page is None or not page.resident or page.ppn is None:
            return
        if page.dirty:
            self.swap_write_delay += self.swap_write_per_page_s
        self.free_ppn.append(page.ppn)
        page.ppn = None
        page.resident = False
        page.dirty = False
        page.pinned = False

    def _evict(self, now: float) -> None:
        residents = [
            p
            for table in self.tables.values()
            for p in table.values()
            if p.resident and not p.pinned
        ]
        if not residents:
            return
        victim = min(residents, key=lambda p: p.last_access)
        if victim.dirty:
            # Writing a dirty frame back to host swap is not free.
            self.swap_write_delay += self.swap_write_per_page_s
        if victim.ppn is not None:
            self.free_ppn.append(victim.ppn)
        victim.ppn = None
        victim.resident = False
        victim.dirty = False
        victim.pinned = False
        self.swap_outs += 1

    def balloon_reclaim(self, tenant: str, pages: int) -> int:
        table = self.tables.get(tenant, {})
        reclaimed = 0
        for p in sorted(table.values(), key=lambda x: x.last_access):
            if reclaimed >= pages:
                break
            if p.resident and p.ppn is not None and not p.pinned:
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
