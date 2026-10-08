from __future__ import annotations

from typing import Dict, List

from llmos.errors import AllocError
from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator


class PageTable:
    def __init__(self, allocator: BlockAllocator) -> None:
        self.allocator = allocator
        self._next_seq = 1
        self.seqs: Dict[int, List[int]] = {}
        self.token_len: Dict[int, int] = {}
        self.mode = "paged"  # or "contiguous"

    def new_sequence(self, max_tokens: int | None = None) -> int:
        seq_id = self._next_seq
        self._next_seq += 1
        if self.mode == "contiguous":
            if max_tokens is None:
                raise AllocError("contiguous mode requires max_tokens")
            n = (max_tokens + self.allocator.block_size - 1) // self.allocator.block_size
            self.seqs[seq_id] = self.allocator.allocate(n)
            self.token_len[seq_id] = 0
        else:
            self.seqs[seq_id] = []
            self.token_len[seq_id] = 0
        return seq_id

    def append_token(self, seq_id: int) -> None:
        if seq_id not in self.seqs:
            raise KeyError(seq_id)
        bs = self.allocator.block_size
        length = self.token_len[seq_id]
        mapping = self.seqs[seq_id]
        if self.mode == "contiguous":
            if length >= len(mapping) * bs:
                raise AllocError("contiguous preallocation exhausted")
            bid = self.allocator.ensure_resident(mapping[length // bs], write=True)
            mapping[length // bs] = bid
            b = self.allocator.blocks[bid]
            b.last_access = self.allocator.now
            b.used_tokens = (length % bs) + 1
            self.token_len[seq_id] = length + 1
            return

        if length % bs == 0:
            new_blocks = self.allocator.allocate(1)
            mapping.append(new_blocks[0])
        else:
            last = mapping[-1]
            if self.allocator.blocks[last].is_shared or self.allocator.blocks[last].ref_count > 1:
                mapping[-1] = self.allocator.cow_on_write(last)
        bid = self.allocator.ensure_resident(mapping[-1], write=True)
        mapping[-1] = bid
        b = self.allocator.blocks[bid]
        b.last_access = self.allocator.now
        b.used_tokens = (length % bs) + 1
        self.token_len[seq_id] = length + 1

    def translate(self, seq_id: int, logical_idx: int) -> tuple[int, int]:
        if seq_id not in self.seqs:
            raise KeyError(seq_id)
        if logical_idx < 0 or logical_idx >= self.token_len[seq_id]:
            raise IndexError(logical_idx)
        bs = self.allocator.block_size
        mapping = self.seqs[seq_id]
        bid = self.allocator.ensure_resident(mapping[logical_idx // bs], write=False)
        mapping[logical_idx // bs] = bid
        return bid, logical_idx % bs

    def fork(self, src_seq: int) -> int:
        if src_seq not in self.seqs:
            raise KeyError(src_seq)
        dst = self._next_seq
        self._next_seq += 1
        copied: list[int] = []
        for bid in self.seqs[src_seq]:
            copied.append(self.allocator.fork(bid))
        self.seqs[dst] = copied
        self.token_len[dst] = self.token_len[src_seq]
        return dst

    def free(self, seq_id: int) -> None:
        if seq_id not in self.seqs:
            return
        self.allocator.free(list(self.seqs[seq_id]))
        del self.seqs[seq_id]
        del self.token_len[seq_id]
