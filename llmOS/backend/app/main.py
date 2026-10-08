from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import db as database
from llmos.experiments import EXPERIMENTS, run_experiment

app = FastAPI(title="llmOS API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    exp_id: str = Field(..., examples=["E1"])
    seed: int = 42


@app.on_event("startup")
def on_startup() -> None:
    database.init_db()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "llmos-backend", "db": database.DB_BACKEND}


@app.get("/api/experiments")
def list_experiments() -> dict[str, Any]:
    catalog = {
        "E1": "连续预占 vs KV 分页",
        "E2": "分页 vs 分页 + COW",
        "E3": "分页 vs 分页 + LRU 换出",
        "E4": "静态批 vs 持续批 FCFS",
        "E5": "FCFS vs SJF vs 抢占",
        "E6": "FCFS vs FAIR 多租户",
        "E7": "串行加载 vs 双缓冲预取",
        "E8": "无配额越界 vs 显存硬上限",
        "E9": "无超卖 vs 超卖 + balloon",
        "E10": "MPS 份额 1:1 vs 3:1",
        "E11": "长上下文 OOM 比例",
        "E12": "单卡可承载模型规模上限",
        "E13": "单卡租户数提升",
    }
    return {"items": [{"id": k, "title": v} for k, v in catalog.items() if k in EXPERIMENTS]}


@app.post("/api/experiments/run")
def run_exp(body: RunRequest) -> dict[str, Any]:
    exp_id = body.exp_id.upper()
    if exp_id not in EXPERIMENTS:
        raise HTTPException(400, f"unknown experiment {exp_id}")
    result = run_experiment(exp_id, seed=body.seed)
    db = database.SessionLocal()
    try:
        row = database.ExperimentRun(
            exp_id=result["id"],
            title=result["title"],
            seed=body.seed,
            verdict=result.get("verdict"),
            payload=result,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"run_id": row.id, "result": result}
    finally:
        db.close()


@app.get("/api/runs")
def list_runs(limit: int = 50) -> dict[str, Any]:
    db = database.SessionLocal()
    try:
        rows = (
            db.query(database.ExperimentRun)
            .order_by(ExperimentRun.id.desc())
            .limit(limit)
            .all()
        )
        return {
            "items": [
                {
                    "run_id": r.id,
                    "exp_id": r.exp_id,
                    "title": r.title,
                    "seed": r.seed,
                    "verdict": r.verdict,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    db = database.SessionLocal()
    try:
        row = db.get(database.ExperimentRun, run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        return {
            "run_id": row.id,
            "exp_id": row.exp_id,
            "title": row.title,
            "seed": row.seed,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "result": row.payload,
        }
    finally:
        db.close()
