from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

_DEFAULT = Path(__file__).resolve().parents[1] / "configs" / "default.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT
    with p.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config must be a mapping")
    return cfg


def config_digest(cfg: dict[str, Any]) -> str:
    blob = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
