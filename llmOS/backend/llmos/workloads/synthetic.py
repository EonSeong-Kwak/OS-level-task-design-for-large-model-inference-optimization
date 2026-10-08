from __future__ import annotations

import random
from typing import Any, List

from llmos.task2_scheduler.request import Request


def synthetic_poisson(
    n: int,
    seed: int = 42,
    arrival_rate: float = 40.0,
    prompt_range: tuple[int, int] = (32, 128),
    gen_range: tuple[int, int] = (32, 128),
    tenants: tuple[str, ...] = ("alpha", "beta"),
) -> List[Request]:
    rng = random.Random(seed)
    t = 0.0
    out: list[Request] = []
    for i in range(n):
        t += rng.expovariate(arrival_rate)
        tenant = tenants[i % len(tenants)]
        out.append(
            Request(
                rid=i + 1,
                arrival_time=t,
                prompt_len=rng.randint(*prompt_range),
                max_tokens=rng.randint(*gen_range),
                priority=rng.randint(0, 2),
                tenant=tenant,
            )
        )
    return out


def from_cfg(cfg: dict[str, Any], which: str, seed: int | None = None) -> List[Request]:
    w = cfg[which]
    pr = tuple(w["prompt_range"])
    gr = tuple(w["gen_range"])
    return synthetic_poisson(
        n=int(w["n"]),
        seed=cfg["seed"] if seed is None else seed,
        arrival_rate=float(w["arrival_rate"]),
        prompt_range=(int(pr[0]), int(pr[1])),
        gen_range=(int(gr[0]), int(gr[1])),
    )


def long_context(n: int, seed: int = 42) -> List[Request]:
    return synthetic_poisson(
        n,
        seed=seed,
        arrival_rate=6.0,
        prompt_range=(1024, 2048),
        gen_range=(128, 256),
    )
