from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from llmos.errors import AllocError, IsolationError


@dataclass
class Block:
    block_id: int
    ref_count: int = 0
    is_shared: bool = False
    last_access: float = 0.0
    resident: bool = True
    swap_location: Optional[int] = None
    used_tokens: int = 0


class BlockAllocator:
    def __init__(self, num_blocks: int, block_size: int, vmm=None) -> None:
        if num_blocks <= 0 or block_size <= 0:
            raise ValueError("num_blocks and block_size must be positive")
        self.num_blocks = num_blocks
        self.block_size = block_size
        self.vmm = vmm
        self.tenant = "default"
        self.blocks = [Block(block_id=i) for i in range(num_blocks)]
        self._free: list[int] = list(range(num_blocks))
        self._swap_seq = 0
        self.evictions = 0
        self.page_faults = 0
        self.now = 0.0

    def allocate(self, n: int) -> List[int]:
        if n <= 0:
            return []
        out: list[int] = []
        for _ in range(n):
            if not self._free:
                stolen = self.steal_for_eviction(self.now, exclude=set(out))
                if stolen is None:
                    for bid in out:
                        self.free([bid])
                    raise AllocError("no free KV blocks after eviction")
            bid = self._free.pop()
            if self.vmm is not None:
                try:
                    self.vmm.access(self.tenant, bid, True, self.now)
                except IsolationError as exc:
                    self._free.append(bid)
                    for done in out:
                        self.free([done])
                    raise AllocError(str(exc)) from exc
            b = self.blocks[bid]
            b.ref_count = 1
            b.is_shared = False
            b.resident = True
            b.swap_location = None
            b.used_tokens = 0
            b.last_access = self.now
            out.append(bid)
        return out

    def free(self, block_ids: List[int]) -> None:
        for bid in block_ids:
            b = self.blocks[bid]
            if b.ref_count <= 0:
                continue
            b.ref_count -= 1
            if b.ref_count == 0:
                b.is_shared = False
                b.resident = True
                b.used_tokens = 0
                if bid not in self._free:
                    self._free.append(bid)

    def fork(self, src: int) -> int:
        b = self.blocks[src]
        if b.ref_count <= 0:
            raise AllocError(f"cannot fork free block {src}")
        b.ref_count += 1
        b.is_shared = True
        return src

    def cow_on_write(self, src: int) -> int:
        b = self.blocks[src]
        if b.ref_count <= 1:
            b.is_shared = False
            return src
        new_id = self.allocate(1)[0]
        self.blocks[new_id].used_tokens = b.used_tokens
        b.ref_count -= 1
        if b.ref_count == 1:
            b.is_shared = False
        return new_id

    def steal_for_eviction(self, now: float, exclude: Optional[set[int]] = None) -> Optional[int]:
        skip = exclude or set()
        candidates = [
            b
            for b in self.blocks
            if b.ref_count == 1
            and b.resident
            and b.block_id not in self._free
            and b.block_id not in skip
        ]
        if not candidates:
            return None
        victim = min(candidates, key=lambda x: x.last_access)
        victim.resident = False
        victim.swap_location = self._swap_seq
        self._swap_seq += 1
        victim.ref_count = 0
        self._free.append(victim.block_id)
        self.evictions += 1
        return victim.block_id

    def ensure_resident(self, block_id: int, write: bool = True) -> int:
        """Page-fault path: ask VMM for a resident frame; restore swapped KV blocks."""
        if block_id < 0 or block_id >= self.num_blocks:
            raise AllocError(f"bad KV block {block_id}")
        b = self.blocks[block_id]
        if b.ref_count > 0 and b.resident:
            self._pin(block_id, write)
            b.last_access = self.now
            return block_id
        self.page_faults += 1
        if b.ref_count == 0 and block_id in self._free:
            self._free.remove(block_id)
            b.ref_count = 1
        elif b.ref_count == 0:
            return self.allocate(1)[0]
        b.resident = True
        b.swap_location = None
        self._pin(block_id, write)
        b.last_access = self.now
        return block_id

    def _pin(self, block_id: int, write: bool) -> None:
        if self.vmm is None:
            return
        try:
            self.vmm.access(self.tenant, block_id, write, self.now)
        except IsolationError as exc:
            raise AllocError(str(exc)) from exc

    def fragmentation_ratio(self) -> float:
        used = self.num_blocks - len(self._free)
        if used == 0:
            return 0.0
        internal = 0
        for b in self.blocks:
            if b.ref_count > 0 and b.resident:
                internal += max(0, self.block_size - b.used_tokens)
        return internal / (used * self.block_size)

    def utilization(self) -> float:
        used_tokens = sum(b.used_tokens for b in self.blocks if b.ref_count > 0)
        return used_tokens / (self.num_blocks * self.block_size)
