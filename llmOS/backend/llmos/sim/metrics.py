from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricsBus:
    series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    counters: dict[str, float] = field(default_factory=dict)
    samples: dict[str, list[float]] = field(default_factory=dict)

    def gauge(self, name: str, t: float, value: float) -> None:
        self.series.setdefault(name, []).append((t, value))

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] = self.counters.get(name, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        self.samples.setdefault(name, []).append(value)

    def percentile(self, name: str, p: float) -> float:
        xs = sorted(self.samples.get(name, []))
        if not xs:
            return 0.0
        idx = min(len(xs) - 1, max(0, int(round((p / 100.0) * (len(xs) - 1)))))
        return xs[idx]

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "percentiles": {
                name: {
                    "p50": self.percentile(name, 50),
                    "p99": self.percentile(name, 99),
                    "mean": sum(xs) / len(xs) if xs else 0.0,
                    "n": len(xs),
                }
                for name, xs in self.samples.items()
            },
            "series": {k: v[-400:] for k, v in self.series.items()},
        }
