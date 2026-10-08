from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Callable


@dataclass(order=True)
class _Event:
    time: float
    priority: int
    seq: int
    fn: Callable[[], None] = field(compare=False)
    kind: str = field(default="", compare=False)


class DiscreteEventSim:
    """Single global clock. All time advance goes through this queue."""

    def __init__(self) -> None:
        self._now = 0.0
        self._seq = 0
        self._heap: list[_Event] = []
        self.last_kind: str = ""

    @property
    def now(self) -> float:
        return self._now

    def schedule(
        self,
        delay: float,
        fn: Callable[[], None],
        priority: int = 0,
        kind: str = "",
    ) -> None:
        if delay < 0:
            raise ValueError("delay must be >= 0")
        heapq.heappush(self._heap, _Event(self._now + delay, priority, self._seq, fn, kind))
        self._seq += 1

    def step(self) -> bool:
        if not self._heap:
            return False
        ev = heapq.heappop(self._heap)
        self._now = ev.time
        self.last_kind = ev.kind
        ev.fn()
        return True

    def run(self, until: float) -> None:
        while self._heap and self._heap[0].time <= until:
            self.step()
        if until > self._now:
            self._now = until
