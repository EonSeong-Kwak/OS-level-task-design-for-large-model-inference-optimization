from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional

from llmos.sim.clock import DiscreteEventSim
from llmos.task3_runtime.storage_model import BandwidthModel


class PrefetchRuntime:
    """Overlap compute with DMA using the discrete-event clock. No wall-clock sleep."""

    def __init__(
        self,
        bw: BandwidthModel,
        layer_weights_gb: List[float],
        lookahead: int = 2,
        cache_layers: int = 2,
        compute_s_per_gb: float = 0.02,
    ) -> None:
        self.bw = bw
        self.layers = layer_weights_gb
        self.lookahead = lookahead
        self.cache_layers = cache_layers
        self.compute_s_per_gb = compute_s_per_gb
        self._cache: OrderedDict[int, float] = OrderedDict()
        self.reset()

    def reset(self) -> None:
        self._cache.clear()
        self._issued: dict[int, float] = {}
        self._dma_free = 0.0
        self._cursor = 0
        self.stall = 0.0
        self.compute = 0.0
        self.timeline: list[dict] = []

    def _cache_put(self, layer: int) -> None:
        if self.cache_layers <= 0:
            return
        self._cache[layer] = self.layers[layer]
        self._cache.move_to_end(layer)
        while len(self._cache) > self.cache_layers:
            self._cache.popitem(last=False)

    def _issue(self, layer: int, now: float, sim: DiscreteEventSim) -> None:
        n = len(self.layers)
        if layer < 0 or layer >= n or layer in self._issued:
            return
        delay = 0.0 if layer in self._cache else self.bw.transfer_time(self.layers[layer], "nvme", "gpu")
        start = max(now, self._dma_free)
        done_at = start + delay
        self._dma_free = done_at
        self._issued[layer] = done_at

        def _done(ly=layer) -> None:
            self._cache_put(ly)

        sim.schedule(max(0.0, done_at - sim.now), _done, kind="DmaComplete")

    def tick(self, sim: DiscreteEventSim) -> float:
        """Issue next-layer DMA on this decode slice; return stall until current layer is ready."""
        n = len(self.layers)
        if n == 0:
            return 0.0
        i = self._cursor % n
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
        self.timeline.append({"layer": i, "compute": compute, "stall": wait, "total": wait + compute})
        self._cursor += 1
        return wait

    def run(self, sim: Optional[DiscreteEventSim] = None) -> dict:
        sim = sim or DiscreteEventSim()
        self.reset()
        n = len(self.layers)
        t = 0.0
        for _ in range(n):
            sim.run(t)
            wait = self.tick(sim)
            t = sim.now + wait + self.layers[(self._cursor - 1) % n] * self.compute_s_per_gb
            sim.run(t)
        sim.run(max(t, self._dma_free, sim.now))
        return {"timeline": self.timeline, **self.metrics()}

    def io_stall_ratio(self) -> float:
        denom = self.compute + self.stall
        return self.stall / denom if denom else 0.0

    def metrics(self) -> dict:
        total = self.compute + self.stall
        return {
            "io_stall_ratio": self.io_stall_ratio(),
            "compute": self.compute,
            "stall": self.stall,
            "effective_compute_ratio": self.compute / total if total else 0.0,
        }
