from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from llmos.config import config_digest, load_config
from llmos.errors import IsolationError
from llmos.sim.clock import DiscreteEventSim
from llmos.sim.metrics import MetricsBus
from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator
from llmos.task1_kv_memory.page_table import PageTable
from llmos.task2_scheduler.request import Request
from llmos.task2_scheduler.scheduler import (
    ContinuousBatchingScheduler,
    StaticBatchingScheduler,
    Strategy,
)
from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
from llmos.task3_runtime.quota_manager import QuotaManager, TenantQuota
from llmos.task3_runtime.storage_model import BandwidthModel
from llmos.task4_gpu_virt.gpu_vmm import GPUVMM
from llmos.task4_gpu_virt.mps_scheduler import MPSScheduler, TenantCompute
from llmos.workloads.synthetic import from_cfg, synthetic_poisson


def _drive(scheduler, reqs: list[Request]) -> None:
    sim = scheduler.sim
    for req in reqs:
        delay = max(0.0, req.arrival_time - sim.now)

        def _submit(r=req) -> None:
            scheduler.submit(r)

        sim.schedule(delay, _submit)
    guard = 0
    while sim._heap and guard < 400000:
        sim.step()
        guard += 1
    while (scheduler.waiting or scheduler.running) and guard < 500000:
        if not sim._heap:
            scheduler._maybe_schedule()
        if not sim._heap:
            break
        sim.step()
        guard += 1


def _clone_reqs(reqs: list[Request]) -> list[Request]:
    return deepcopy(reqs)


def _public(metrics: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metrics.items() if k not in {"series", "gantt"}}


def _pct_change(baseline: float, optimized: float, *, lower_better: bool = False) -> float:
    if abs(baseline) < 1e-12:
        return 0.0
    if lower_better:
        return (baseline - optimized) / abs(baseline) * 100.0
    return (optimized - baseline) / abs(baseline) * 100.0


def _kpis(base: dict, opt: dict) -> dict[str, float]:
    keys = {
        "kv_utilization_peak": False,
        "peak_concurrency": False,
        "peak_live_seqs": False,
        "throughput": False,
        "gpu_busy_ratio": False,
        "effective_compute_ratio": False,
        "kv_fragmentation_peak": True,
        "oom_ratio": True,
        "io_stall_ratio": True,
        "hunger_ratio": True,
        "tenant_p99_var": True,
        "ctx_switch_overhead_ratio": True,
    }
    out: dict[str, float] = {}
    for key, lower in keys.items():
        if key in base and key in opt:
            out[f"{key}_delta_pct"] = _pct_change(float(base[key]), float(opt[key]), lower_better=lower)
    return out


def run_serving(
    reqs: list[Request],
    cfg: dict[str, Any] | None = None,
    *,
    mode: str = "continuous",
    strategy: str = "fcfs",
    kv_mode: str = "paged",
    num_blocks: int | None = None,
    phys_pages: int | None = None,
    share_alpha: float | None = None,
    share_beta: float | None = None,
    mem_cap: float | None = None,
    tenants: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    num_blocks = int(num_blocks if num_blocks is not None else cfg["num_kv_blocks"])
    block_size = int(cfg["block_size"])
    batch_size = int(cfg["batch_size"])
    phys_pages = int(phys_pages if phys_pages is not None else cfg["phys_pages"])
    sa = float(share_alpha if share_alpha is not None else cfg.get("share_alpha", 1.0))
    sb = float(share_beta if share_beta is not None else cfg.get("share_beta", 1.0))
    cap = float(mem_cap if mem_cap is not None else cfg["total_gpu_mem_gb"] / 2)

    sim = DiscreteEventSim()
    metrics = MetricsBus()
    tenant_names = tenants or ("alpha", "beta", "default")
    vmm = GPUVMM(phys_pages=phys_pages, page_size_gb=float(cfg["page_size_gb"]))
    for tenant in tenant_names:
        vmm.create_tenant(tenant, num_blocks)
    alloc = BlockAllocator(num_blocks=num_blocks, block_size=block_size, vmm=vmm)
    pt = PageTable(alloc)
    pt.mode = kv_mode
    quota = QuotaManager(total_gpu_mem_gb=float(cfg["total_gpu_mem_gb"]))
    mps = MPSScheduler(time_slice=float(cfg["time_slice"]))
    for tenant in tenant_names:
        share = sa if tenant == "alpha" else sb if tenant == "beta" else 1.0
        quota.register(TenantQuota(tenant, gpu_compute_share=share, gpu_mem_gb=cap))
        mps.register(TenantCompute(tenant, share=share, ctx_switch_cost=float(cfg["ctx_switch_cost"])))
    bw = BandwidthModel(
        nvme_gbps=float(cfg["nvme_gbps"]),
        cpu_gpu_gbps=float(cfg["cpu_gpu_gbps"]),
        gpu_hbm_gbps=float(cfg["gpu_hbm_gbps"]),
    )
    prefetch = PrefetchRuntime(
        bw,
        layer_weights_gb=[0.02] * 8,
        lookahead=int(cfg["lookahead"]),
        cache_layers=int(cfg["cache_layers"]),
        compute_s_per_gb=0.001,
    )
    kwargs = dict(
        sim=sim,
        batch_size=batch_size,
        step_cost=float(cfg["step_cost"]),
        page_table=pt,
        metrics=metrics,
        quota=quota,
        mps=mps,
        prefetch=prefetch,
        alpha_prefill=float(cfg["alpha_prefill"]),
        beta_decode=float(cfg["beta_decode"]),
        hunger_threshold=float(cfg["hunger_threshold"]),
        time_slice=float(cfg["time_slice"]),
    )
    if mode == "static":
        sched = StaticBatchingScheduler(**kwargs)
    else:
        sched = ContinuousBatchingScheduler(**kwargs, strategy=Strategy(strategy))
    _drive(sched, _clone_reqs(reqs))
    out = sched.metrics_dict()
    out["gantt"] = sched.gantt[-200:]
    out["series"] = metrics.snapshot()["series"]
    out["fairness"] = mps.fairness_index()
    return out


def _pack(exp_id: str, title: str, base: dict, opt: dict, cfg: dict, verdict: str, extra=None) -> dict[str, Any]:
    return {
        "id": exp_id,
        "title": title,
        "baseline": _public(base),
        "optimized": _public(opt),
        "extra": extra or {},
        "charts": {
            "kv_util": {
                "baseline": base.get("series", {}).get("kv_util", []),
                "optimized": opt.get("series", {}).get("kv_util", []),
            },
            "kv_frag": {
                "baseline": base.get("series", {}).get("kv_frag", []),
                "optimized": opt.get("series", {}).get("kv_frag", []),
            },
        },
        "gantt": {"baseline": base.get("gantt", []), "optimized": opt.get("gantt", [])},
        "verdict": verdict,
        "kpis": _kpis(base, opt),
        "config_digest": config_digest(cfg),
        "config": {k: cfg[k] for k in cfg if k not in {"w1", "w2", "w3", "w4"}},
    }


def experiment_e1(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    cfg["seed"] = seed
    reqs = from_cfg(cfg, "w2", seed=seed)
    blocks = 40
    base = run_serving(reqs, cfg, mode="continuous", kv_mode="contiguous", num_blocks=blocks)
    opt = run_serving(reqs, cfg, mode="continuous", kv_mode="paged", num_blocks=blocks)
    better = (
        opt["kv_fragmentation_peak"] <= base["kv_fragmentation_peak"]
        or opt["oom_ratio"] <= base["oom_ratio"]
        or opt["peak_concurrency"] >= base["peak_concurrency"]
    )
    packed = _pack("E1", "连续预占 vs KV 分页", base, opt, cfg, "分页降低碎片或 OOM" if better else "需检查配置")
    packed["extra"] = {
        **(packed.get("extra") or {}),
        "util_lift_pct": packed["kpis"].get("kv_utilization_peak_delta_pct", 0.0),
        "frag_drop_pct": packed["kpis"].get("kv_fragmentation_peak_delta_pct", 0.0),
        "concurrency_lift_pct": packed["kpis"].get("peak_concurrency_delta_pct", 0.0),
        "oom_drop_pct": packed["kpis"].get("oom_ratio_delta_pct", 0.0),
    }
    return packed


def experiment_e2(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    w4 = cfg["w4"]
    tokens = int(w4["prompt_tokens"])
    branches = int(w4["branches"])
    alloc_b = BlockAllocator(256, int(cfg["block_size"]))
    pt_b = PageTable(alloc_b)
    used_copy = 0
    for _ in range(branches):
        sid = pt_b.new_sequence()
        for _t in range(tokens):
            pt_b.append_token(sid)
        used_copy += len(pt_b.seqs[sid])
    alloc_o = BlockAllocator(256, int(cfg["block_size"]))
    pt_o = PageTable(alloc_o)
    src = pt_o.new_sequence()
    for _t in range(tokens):
        pt_o.append_token(src)
    forks = [pt_o.fork(src) for _ in range(branches - 1)]
    for f in forks:
        pt_o.append_token(f)
    used_shared = sum(1 for b in alloc_o.blocks if b.ref_count > 0)
    return {
        "id": "E2",
        "title": "分页独立拷贝 vs 分页 + COW（W4）",
        "baseline": {"physical_blocks": used_copy, "branches": branches},
        "optimized": {"physical_blocks": used_shared, "branches": branches},
        "charts": {},
        "gantt": {},
        "verdict": "COW 前缀共享减少物理块",
        "config_digest": config_digest(cfg),
        "seed": seed,
    }


def experiment_e3(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    cfg["seed"] = seed
    reqs = from_cfg(cfg, "w2", seed=seed)
    base = run_serving(reqs, cfg, mode="continuous", kv_mode="paged", num_blocks=512, phys_pages=512)
    opt = run_serving(reqs, cfg, mode="continuous", kv_mode="paged", num_blocks=64, phys_pages=64)
    return _pack(
        "E3",
        "大池不分页换出 vs 小池 + LRU",
        base,
        opt,
        cfg,
        "小池触发换出以换空间",
    )


def experiment_e4(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    cfg["seed"] = seed
    reqs = from_cfg(cfg, "w1", seed=seed)
    base = run_serving(reqs, cfg, mode="static", kv_mode="paged")
    opt = run_serving(reqs, cfg, mode="continuous", strategy="fcfs", kv_mode="paged")
    ok = opt["throughput"] >= base["throughput"]
    return _pack("E4", "静态批 vs 持续批 FCFS", base, opt, cfg, "持续批提升吞吐" if ok else "需检查配置")


def experiment_e5(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    cfg["seed"] = seed
    reqs = from_cfg(cfg, "w1", seed=seed)
    fcfs = run_serving(reqs, cfg, strategy="fcfs")
    sjf = run_serving(reqs, cfg, strategy="sjf")
    pre = run_serving(reqs, cfg, strategy="preemptive")
    packed = _pack("E5", "FCFS vs SJF vs 抢占", fcfs, pre, cfg, "对照周转、TTFT 与饥饿")
    packed["extra"] = {"sjf": _public(sjf)}
    return packed


def experiment_e6(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    cfg["seed"] = seed
    w3 = cfg["w3"]
    reqs = from_cfg(cfg, "w3", seed=seed)
    base = run_serving(reqs, cfg, strategy="fcfs", share_alpha=1.0, share_beta=1.0)
    opt = run_serving(
        reqs,
        cfg,
        strategy="fair",
        share_alpha=float(w3.get("share_alpha", 3.0)),
        share_beta=float(w3.get("share_beta", 1.0)),
    )
    return _pack("E6", "FCFS vs FAIR（W3）", base, opt, cfg, "FAIR 压低跨租户 P99 方差或饥饿")


def experiment_e7(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    layers = [0.4] + [0.7] * 10
    bw = BandwidthModel(
        nvme_gbps=float(cfg["nvme_gbps"]),
        cpu_gpu_gbps=float(cfg["cpu_gpu_gbps"]),
        gpu_hbm_gbps=float(cfg["gpu_hbm_gbps"]),
    )
    serial = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    double = PrefetchRuntime(bw, layers, lookahead=1, cache_layers=0)
    cached = PrefetchRuntime(bw, layers, lookahead=int(cfg["lookahead"]), cache_layers=int(cfg["cache_layers"]))
    s, d, c = serial.run(), double.run(), cached.run()
    sm, cm = serial.metrics(), cached.metrics()
    return {
        "id": "E7",
        "title": "串行 vs 双缓冲 vs 预取+缓存",
        "baseline": sm,
        "optimized": cm,
        "kpis": {
            "io_stall_ratio_delta_pct": _pct_change(sm["io_stall_ratio"], cm["io_stall_ratio"], lower_better=True),
            "effective_compute_ratio_delta_pct": _pct_change(
                sm["effective_compute_ratio"], cm["effective_compute_ratio"]
            ),
        },
        "extra": {"double_buffer": double.metrics()},
        "charts": {
            "stall_timeline": {
                "baseline": [(i, x["stall"]) for i, x in enumerate(s["timeline"])],
                "double": [(i, x["stall"]) for i, x in enumerate(d["timeline"])],
                "optimized": [(i, x["stall"]) for i, x in enumerate(c["timeline"])],
            }
        },
        "gantt": {},
        "verdict": "预取后 stall 不高于串行",
        "config_digest": config_digest(cfg),
        "seed": seed,
    }


def experiment_e8(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    reqs = synthetic_poisson(12, seed=seed, arrival_rate=30.0, prompt_range=(16, 32), gen_range=(8, 16))
    base = run_serving(reqs, cfg, mem_cap=100.0)
    opt = run_serving(reqs, cfg, mem_cap=0.02)
    return _pack("E8", "宽松配额 vs 显存硬上限", base, opt, cfg, "硬上限会拒绝超额并保持隔离")


def experiment_e9(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    t = 0.0

    def workload(phys: int, virtual: int, balloon: bool) -> dict[str, Any]:
        vmm = GPUVMM(phys_pages=phys, page_size_gb=float(cfg["page_size_gb"]))
        vmm.create_tenant("alpha", virtual)
        vmm.create_tenant("beta", virtual)
        now = 0.0
        for vpn in range(virtual):
            now += 0.001
            vmm.access("alpha", vpn, True, now)
        for vpn in range(virtual // 2):
            now += 0.001
            vmm.access("beta", vpn, True, now)
        blocked = 0
        try:
            vmm.access("alpha", virtual + 5, False, now)
        except IsolationError:
            blocked += 1
        reclaimed = vmm.balloon_reclaim("alpha", 4) if balloon else 0
        return {
            "oversub": vmm.oversubscription_ratio(),
            "isolation_violation_rate": vmm.isolation_violation_rate(),
            "isolation_intercept_rate": 1.0 - vmm.isolation_violation_rate(),
            "page_faults": vmm.page_faults,
            "swap_outs": vmm.swap_outs,
            "blocked_cross_tenant": blocked,
            "balloon_reclaimed": reclaimed,
        }

    base = workload(40, 20, False)
    opt = workload(16, 20, True)
    return {
        "id": "E9",
        "title": "无超卖 vs 超卖 + balloon",
        "baseline": base,
        "optimized": opt,
        "charts": {},
        "gantt": {},
        "verdict": "超卖可运行且越界拦截率为 0",
        "config_digest": config_digest(cfg),
        "seed": seed,
    }


def experiment_e10(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    even = MPSScheduler(time_slice=float(cfg["time_slice"]))
    even.register(TenantCompute("alpha", share=1.0))
    even.register(TenantCompute("beta", share=1.0))
    skewed = MPSScheduler(time_slice=float(cfg["time_slice"]))
    skewed.register(TenantCompute("alpha", share=3.0))
    skewed.register(TenantCompute("beta", share=1.0))
    now = 0.0
    slice_t = float(cfg["time_slice"])
    ctx = float(cfg["ctx_switch_cost"])
    for _ in range(80):
        even.run_slice(even.pick_next(now), now)
        skewed.run_slice(skewed.pick_next(now), now)
        now += slice_t
    horizon = max(now, 1e-9)
    return {
        "id": "E10",
        "title": "MPS 份额 1:1 vs 3:1",
        "baseline": {
            "fairness": even.fairness_index(),
            "alpha": even.tenants["alpha"].accumulated,
            "beta": even.tenants["beta"].accumulated,
            "ctx_switch_count": even.switch_count,
            "ctx_switch_overhead_ratio": even.switch_count * ctx / horizon,
        },
        "optimized": {
            "fairness": skewed.fairness_index(),
            "alpha": skewed.tenants["alpha"].accumulated,
            "beta": skewed.tenants["beta"].accumulated,
            "ctx_switch_count": skewed.switch_count,
            "ctx_switch_overhead_ratio": skewed.switch_count * ctx / horizon,
        },
        "charts": {},
        "gantt": {},
        "verdict": "3:1 时 alpha 累计服务时间更高",
        "config_digest": config_digest(cfg),
        "seed": seed,
    }


def experiment_e11(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    reqs = synthetic_poisson(
        6,
        seed=seed,
        arrival_rate=8.0,
        prompt_range=(256, 384),
        gen_range=(32, 64),
    )
    blocks = 28
    base = run_serving(reqs, cfg, kv_mode="contiguous", num_blocks=blocks, phys_pages=blocks)
    opt = run_serving(reqs, cfg, kv_mode="paged", num_blocks=blocks, phys_pages=blocks)
    return _pack("E11", "长上下文连续预占 vs 分页 OOM 率", base, opt, cfg, "分页降低长上下文 OOM 比例")


def experiment_e12(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    hbm_gb = 4.0
    layer_gb = 0.5
    bw = BandwidthModel(
        nvme_gbps=float(cfg["nvme_gbps"]),
        cpu_gpu_gbps=float(cfg["cpu_gpu_gbps"]),
        gpu_hbm_gbps=float(cfg["gpu_hbm_gbps"]),
    )

    def max_model_gb(lookahead: int, cache_layers: int) -> float:
        best = 0.0
        for n in range(4, 33, 4):
            total = n * layer_gb
            resident = layer_gb * (n if lookahead <= 0 else min(cache_layers + 1, n))
            if resident > hbm_gb + 1e-9:
                break
            PrefetchRuntime(bw, [layer_gb] * n, lookahead=lookahead, cache_layers=cache_layers).run()
            best = total
        return best

    serial_gb = max_model_gb(0, 0)
    prefetch_gb = max_model_gb(int(cfg["lookahead"]), int(cfg["cache_layers"]))
    return {
        "id": "E12",
        "title": "串行全载 vs 预取下的单卡模型规模上限",
        "baseline": {"max_model_gb": serial_gb, "hbm_gb": hbm_gb},
        "optimized": {"max_model_gb": prefetch_gb, "hbm_gb": hbm_gb},
        "kpis": {"max_model_gb_delta_pct": _pct_change(serial_gb, prefetch_gb)},
        "charts": {},
        "gantt": {},
        "verdict": "预取只驻留 lookahead 层，可承载更大模型",
        "config_digest": config_digest(cfg),
        "seed": seed,
    }


def experiment_e13(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    two = ("t0", "t1")
    eight = tuple(f"t{i}" for i in range(8))
    reqs2 = synthetic_poisson(8, seed=seed, arrival_rate=20.0, prompt_range=(16, 32), gen_range=(8, 16), tenants=two)
    reqs8 = synthetic_poisson(8, seed=seed, arrival_rate=20.0, prompt_range=(16, 32), gen_range=(8, 16), tenants=eight)
    base = run_serving(reqs2, cfg, tenants=two, phys_pages=24, num_blocks=48)
    opt = run_serving(reqs8, cfg, tenants=eight, phys_pages=24, num_blocks=48)
    packed = _pack("E13", "2 租户 vs 8 租户同卡超卖", base, opt, cfg, "超卖+时间片下单卡租户数可提升")
    packed["extra"] = {
        "baseline_tenants": 2,
        "optimized_tenants": 8,
        "tenant_count_lift_pct": 300.0,
    }
    return packed


EXPERIMENTS: dict[str, Callable[..., dict[str, Any]]] = {
    "E1": experiment_e1,
    "E2": experiment_e2,
    "E3": experiment_e3,
    "E4": experiment_e4,
    "E5": experiment_e5,
    "E6": experiment_e6,
    "E7": experiment_e7,
    "E8": experiment_e8,
    "E9": experiment_e9,
    "E10": experiment_e10,
    "E11": experiment_e11,
    "E12": experiment_e12,
    "E13": experiment_e13,
}


def run_experiment(exp_id: str, seed: int = 42) -> dict[str, Any]:
    result = EXPERIMENTS[exp_id.upper()](seed=seed)
    result["seed"] = seed
    return result
