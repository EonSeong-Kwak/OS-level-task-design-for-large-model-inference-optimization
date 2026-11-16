from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional

from llmos.sim.clock import DiscreteEventSim
from llmos.task3_runtime.storage_model import BandwidthModel


class _DmaHandle:
    """Awaitable whose wake-up time is already booked on the event heap.

    Awaiting it never suspends the caller: the DMA completion was registered via
    ``DiscreteEventSim.schedule`` beforehand, so this handle only unwraps the
    timestamp (spec §7.1: “把 await 点翻译为 schedule(delay, callback)”).
    """

    __slots__ = ("due",)

    def __init__(self, due: float) -> None:
        self.due = due

    def __await__(self):
        if False:  # pragma: no cover - makes this a generator-based coroutine
            yield
        return self.due


class PrefetchRuntime:
    """Overlap compute with DMA using the discrete-event clock. No wall-clock sleep."""

    def __init__(
        self,
        bw: BandwidthModel,
        layer_weights_gb: List[float],
        lookahead: int = 2,
        cache_layers: int = 2,
        compute_s_per_gb: float = 0.02,
        quota=None,
        cache_tenant: str = "__weights__",
    ) -> None:
        self.bw = bw
        self.layers = layer_weights_gb
        self.lookahead = lookahead
        self.cache_layers = cache_layers
        self.compute_s_per_gb = compute_s_per_gb
        # Resident weight cache is charged against a memory quota (spec 4.3.2),
        # so caching can never grow without bound.
        self.quota = quota
        self.cache_tenant = cache_tenant
        self._cache: OrderedDict[int, float] = OrderedDict()
        self.reset()

    def reset(self) -> None:
        self._cache.clear()
        self._issued: dict[int, float] = {}
        self._dma_free = 0.0
        self._cursor = 0
        self.stall = 0.0
        self.compute = 0.0
        self.cache_hits = 0
        self.cache_misses = 0
        self.timeline: list[dict] = []
        # [start, end, layer] for GPU compute and DMA, used by the overlap chart.
        self.compute_spans: list[tuple[float, float, int]] = []
        self.dma_spans: list[tuple[float, float, int]] = []

    def _cache_put(self, layer: int) -> None:
        if self.cache_layers <= 0:
            return
        if layer not in self._cache:
            size = self.layers[layer]
            # Charge the resident layer against the cache quota; if the quota
            # is exhausted, skip caching (DMA still completes, no residency).
            if self.quota is not None and not self.quota.alloc_mem(self.cache_tenant, size):
                return
        self._cache[layer] = self.layers[layer]
        self._cache.move_to_end(layer)
        while len(self._cache) > self.cache_layers:
            popped, popped_size = self._cache.popitem(last=False)
            if self.quota is not None:
                self.quota.free_mem(self.cache_tenant, popped_size)

    def _issue(self, layer: int, now: float, sim: DiscreteEventSim) -> None:
        n = len(self.layers)
        if layer < 0 or layer >= n or layer in self._issued:
            return
        if layer in self._cache:
            delay = 0.0
            self.cache_hits += 1
        else:
            delay = self.bw.transfer_time(self.layers[layer], "nvme", "gpu")
            self.cache_misses += 1
        start = max(now, self._dma_free)
        done_at = start + delay
        self._dma_free = done_at
        self._issued[layer] = done_at
        if delay > 0:
            self.dma_spans.append((start, done_at, layer))

        def _done(ly=layer) -> None:
            self._cache_put(ly)

        sim.schedule(max(0.0, done_at - sim.now), _done, kind="DmaComplete")

    def tick(self, sim: DiscreteEventSim) -> float:
        """Issue next-layer DMA on this decode slice; return stall until current layer is ready."""
        n = len(self.layers)
        if n == 0:
            return 0.0
        wait = self._advance(sim, self._cursor % n)
        self._cursor += 1
        return wait

    async def decode_step(self, layer: int, sim: Optional[DiscreteEventSim] = None) -> float:
        """Frozen interface (spec §5.3): decode one layer while prefetching ahead.

        Expressed as a coroutine, but the DMA completion is booked on the
        discrete-event heap *before* the await point; awaiting it only unwraps
        an already-decided timestamp, so no wall-clock sleep happens and the
        result never depends on task/thread completion order (spec §7.1/§7.3).
        Returns the measured I/O stall for this layer.
        """
        n = len(self.layers)
        if n == 0:
            return 0.0
        sim = sim if sim is not None else DiscreteEventSim()
        idx = layer % n
        wait = self._advance(sim, idx)
        await _DmaHandle(self._issued.get(idx, sim.now))
        return wait

    def _advance(self, sim: DiscreteEventSim, i: int) -> float:
        """Issue DMA for layer i (and lookahead), book compute/stall, return stall."""
        n = len(self.layers)
        if i == 0:
            self._issued.clear()
        t = sim.now
        span = 1 if self.lookahead <= 0 else self.lookahead + 1
        for j in range(i, min(n, i + span)):
            self._issue(j, t, sim)
        wait = max(0.0, self._issued[i] - t)
        self.stall += wait
        compute = self.layers[i] * self.compute_s_per_gb
        self.compute += compute
        self.compute_spans.append((t + wait, t + wait + compute, i))
        self.timeline.append({"layer": i, "compute": compute, "stall": wait, "total": wait + compute})
        return wait

    def run(self, sim: Optional[DiscreteEventSim] = None, epochs: int = 1) -> dict:
        """Sweep every layer `epochs` times.

        Each epoch is one decode pass over all weights. Real serving reuses
        resident weights on every generated token, so with epochs>1 the LRU
        page cache can actually hit; a single epoch can never demonstrate
        cache value (each layer is touched exactly once).
        """
        sim = sim or DiscreteEventSim()
        self.reset()
        n = len(self.layers)
        t = 0.0
        for k in range(n * max(1, epochs)):
            layer_idx = k % n
            sim.run(t)
            wait = self.tick(sim)
            t = sim.now + wait + self.layers[layer_idx] * self.compute_s_per_gb
            sim.run(t)
        sim.run(max(t, self._dma_free, sim.now))
        return {"timeline": self.timeline, **self.metrics()}

    def io_stall_ratio(self) -> float:
        denom = self.compute + self.stall
        return self.stall / denom if denom else 0.0

    def overlap(self) -> dict:
        """Return GPU-compute and DMA intervals for the compute/IO overlap chart."""
        return {
            "compute": [[s, e] for s, e, _ in self.compute_spans],
            "dma": [[s, e] for s, e, _ in self.dma_spans],
        }

    def metrics(self) -> dict:
        total = self.compute + self.stall
        touches = self.cache_hits + self.cache_misses
        return {
            "io_stall_ratio": self.io_stall_ratio(),
            "compute": self.compute,
            "stall": self.stall,
            "effective_compute_ratio": self.compute / total if total else 0.0,
            "cache_hit_rate": self.cache_hits / touches if touches else 0.0,
            "cache_hits": self.cache_hits,
        }
