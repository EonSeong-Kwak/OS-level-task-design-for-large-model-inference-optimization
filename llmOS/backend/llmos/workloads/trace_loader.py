"""Load real request traces into the simulator (spec 3.4 trace_loader).

Accepts a JSONL/JSON file of pre-tokenized traces such as a preprocessed
ShareGPT export:

    {"arrival_time": 0.012, "input_tokens": 137, "output_tokens": 256,
     "tenant": "alpha", "priority": 0}

Accepted field aliases:
    arrival_time | timestamp | t
    input_tokens | prompt_tokens | prompt_len
    output_tokens | gen_tokens | max_tokens | completion_tokens
    tenant (default rotates alpha/beta) | priority (default 0)

Raw ShareGPT "conversations" text is NOT tokenized here (no tokenizer in the
simulator); pre-tokenize offline first. This keeps the sim deterministic and
free of model/tokenizer dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, List

from llmos.task2_scheduler.request import Request

_ARRIVAL_KEYS = ("arrival_time", "timestamp", "t")
_PROMPT_KEYS = ("input_tokens", "prompt_tokens", "prompt_len")
_GEN_KEYS = ("output_tokens", "gen_tokens", "max_tokens", "completion_tokens")


def _pick(row: dict, keys: Iterable[str], default=None):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def parse_trace_rows(
    rows: Iterable[dict[str, Any]],
    default_gen_tokens: int = 128,
    tenants: tuple[str, ...] = ("alpha", "beta"),
) -> List[Request]:
    reqs: list[Request] = []
    for i, row in enumerate(rows):
        prompt = _pick(row, _PROMPT_KEYS)
        if prompt is None:
            raise ValueError(f"trace row {i}: missing prompt token count ({_PROMPT_KEYS})")
        gen = _pick(row, _GEN_KEYS, default_gen_tokens)
        arrival = float(_pick(row, _ARRIVAL_KEYS, 0.0))
        tenant = _pick(row, ("tenant",))
        if tenant is None:
            tenant = tenants[i % len(tenants)] if tenants else "default"
        reqs.append(
            Request(
                rid=i + 1,
                arrival_time=float(arrival),
                prompt_len=int(prompt),
                max_tokens=int(gen),
                tenant=str(tenant),
                priority=int(_pick(row, ("priority",), 0)),
            )
        )
    reqs.sort(key=lambda r: r.arrival_time)
    return reqs


def load_jsonl_trace(
    path: str | Path,
    default_gen_tokens: int = 128,
    tenants: tuple[str, ...] = ("alpha", "beta"),
) -> List[Request]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] == "[":
        rows = json.loads(stripped)
    else:
        rows = []
        for ln, line in enumerate(stripped.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on trace line {ln}: {exc}") from exc
    return parse_trace_rows(rows, default_gen_tokens=default_gen_tokens, tenants=tenants)
