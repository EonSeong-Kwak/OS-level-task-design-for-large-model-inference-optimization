"""GPU reality-check job control (tech-plan §3.3 / §6.3).

Runs adapters/gpu_g1.py as an ISOLATED subprocess so torch never lives inside
the FastAPI process. State is module-level: the API runs single-worker uvicorn.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/gpu-backing", tags=["gpu-backing"])

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "adapters" / "gpu_g1.py"
RESULTS_DIR = REPO_ROOT / "gpu_results"
LOG_FILE = RESULTS_DIR / "last_run.log"
DEFAULT_MODEL = r"D:\Python\data\Qwen2.5-0.5B"

_state_lock = threading.Lock()
_job: dict[str, Any] = {
    "proc": None,
    "status": "idle",          # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "model": None,
    "mode": None,
}
_cuda_cache: dict[str, Any] = {"ts": 0.0, "available": None, "name": None}


class GpuRunRequest(BaseModel):
    model: str = Field(default=DEFAULT_MODEL)
    mode: str = Field(default="all", pattern="^(g1|calib|all)$")


def _probe_cuda(ttl_s: float = 30.0) -> dict[str, Any]:
    """Cheap cached CUDA probe via a throwaway subprocess."""
    now = time.time()
    if _cuda_cache["available"] is not None and now - _cuda_cache["ts"] < ttl_s:
        return {"available": _cuda_cache["available"], "name": _cuda_cache["name"]}
    code = ("import torch;print(int(torch.cuda.is_available()));"
            "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')")
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = out.stdout.strip().splitlines()
        available = bool(lines and lines[0].strip() == "1")
        name = lines[1].strip() if available and len(lines) > 1 else None
    except Exception:
        available, name = False, None
    _cuda_cache.update(ts=now, available=available, name=name)
    return {"available": available, "name": name}


def _load_json(name: str) -> Any:
    p = RESULTS_DIR / name
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _log_tail(max_lines: int = 40) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-max_lines:]
    except Exception:
        return []


def _reap_if_finished() -> None:
    """Must be called under _state_lock."""
    proc = _job["proc"]
    if proc is None or proc.poll() is None:
        return
    _job["returncode"] = proc.returncode
    _job["status"] = "done" if proc.returncode == 0 else "error"
    _job["finished_at"] = time.time()
    _job["proc"] = None


def _snapshot() -> dict[str, Any]:
    with _state_lock:
        _reap_if_finished()
        snap = {
            "status": _job["status"],
            "started_at": _job["started_at"],
            "finished_at": _job["finished_at"],
            "returncode": _job["returncode"],
            "model": _job["model"],
            "mode": _job["mode"],
            "log": _log_tail(),
            "g1": _load_json("g1.json"),
            "calibration": _load_json("calibration.json"),
        }
    snap.update(_probe_cuda())
    snap["default_model"] = DEFAULT_MODEL
    snap["adapter_exists"] = ADAPTER.exists()
    return snap


@router.get("")
def status() -> dict[str, Any]:
    return _snapshot()


@router.post("/run")
def run_gpu_check(body: GpuRunRequest) -> dict[str, Any]:
    if not ADAPTER.exists():
        raise HTTPException(400, f"adapter not found: {ADAPTER}")
    cuda = _probe_cuda(ttl_s=0.0)
    if not cuda["available"]:
        raise HTTPException(400, "CUDA 不可用：本机检测不到可用 GPU，真机背书无法运行")
    with _state_lock:
        _reap_if_finished()
        if _job["status"] == "running":
            raise HTTPException(409, "GPU check already running")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        log_fh = open(LOG_FILE, "w", encoding="utf-8")
        cmd = [sys.executable, "-u", str(ADAPTER),
               "--model", body.model, "--mode", body.mode]
        child_env = {**os.environ, "PYTHONUNBUFFERED": "1", "TQDM_DISABLE": "1",
                     "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(REPO_ROOT), stdout=log_fh,
                stderr=subprocess.STDOUT, env=child_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as e:
            log_fh.close()
            raise HTTPException(400, f"cannot launch python: {e}") from e
        log_fh.close()  # child keeps its own inherited handle
        _job.update(proc=proc, status="running", started_at=time.time(),
                    finished_at=None, returncode=None,
                    model=body.model, mode=body.mode)
    return {"started": True, "pid": proc.pid, "model": body.model, "mode": body.mode}
