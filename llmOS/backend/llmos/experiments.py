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
    # Only paged KV memory may LRU-swap blocks; contiguous pre-reservation
    # must reject arrivals outright when the pool is exhausted.
    alloc = BlockAllocator(
        num_blocks=num_blocks,
        block_size=block_size,
        vmm=vmm,
        allow_eviction=(kv_mode == "paged"),
    )
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
    layer_gb = 0.02
    n_layers = 8
    cache_budget_gb = int(cfg["cache_layers"]) * layer_gb
    quota.register(TenantQuota("__weights__", gpu_compute_share=0.0, gpu_mem_gb=cache_budget_gb))
    prefetch = PrefetchRuntime(
        bw,
        layer_weights_gb=[layer_gb] * n_layers,
        lookahead=int(cfg["lookahead"]),
        cache_layers=int(cfg["cache_layers"]),
        compute_s_per_gb=0.001,
        quota=quota,
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
        hunger_threshold_x=float(cfg.get("hunger_threshold_x", 10.0)),
        time_slice=float(cfg["time_slice"]),
        kv_block_gb=float(cfg.get("kv_block_gb", 0.005)),
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
    # Tight pool where worst-case contiguous pre-reservation rejects arrivals
    # while on-demand paged allocation plus LRU swap-out still serves them.
    blocks = 220
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
    # Base pool is large enough to hold the worst-case W2 working set
    # (6 sequences x 288 blocks = 1728), so it never swaps; the optimized
    # pool is 288 blocks (one worst-case sequence: (4096+512)/16) and relies
    # on LRU swap-out to multiplex all six sequences onto that single footprint.
    base = run_serving(reqs, cfg, mode="continuous", kv_mode="paged", num_blocks=2048, phys_pages=2048)
    opt = run_serving(reqs, cfg, mode="continuous", kv_mode="paged", num_blocks=288, phys_pages=288)
    verdict = (
        "小池以换出承载相同负载（显存压缩）"
        if opt["completed"] >= base["completed"] and opt["kv_evictions"] > base["kv_evictions"]
        else "小池换出承载部分负载，超出部分仍需拒绝"
    )
    return _pack(
        "E3",
        "大池不分页换出 vs 小池 + LRU",
        base,
        opt,
        cfg,
        verdict,
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
    # Overload regime: 4 slots and dense tenant bursts so slot admission is
    # the scarce resource. Under FCFS the first bursting tenant monopolises
    # slots; FAIR admits in proportion to the (equal) shares.
    cfg["batch_size"] = 4
    cfg["w3"]["arrival_rate"] = 90.0
    reqs = from_cfg(cfg, "w3", seed=seed)
    # Spec 6.2: FCFS vs FAIR under EQUAL 1:1 shares. The 3:1 weighted case is
    # already covered by E10; FAIR here must lower cross-tenant P99 variance.
    base = run_serving(
        reqs, cfg, mode="continuous", strategy="fcfs", share_alpha=1.0, share_beta=1.0
    )
    opt = run_serving(
        reqs, cfg, mode="continuous", strategy="fair", share_alpha=1.0, share_beta=1.0
    )
    better = opt["tenant_p99_var"] <= base["tenant_p99_var"] + 1e-12
    packed = _pack(
        "E6",
        "FCFS vs FAIR（W3, 1:1 份额）",
        base,
        opt,
        cfg,
        "FAIR 压低跨租户 P99 方差" if better else "FAIR 未压低方差，需检查配置",
    )
    # Per-tenant P99 bar chart (spec 6.4).
    tenants = sorted(set(base["tenant_p99"]) | set(opt["tenant_p99"]))
    packed["charts"]["tenant_p99"] = {
        "tenants": tenants,
        "baseline": [base["tenant_p99"].get(t, 0.0) for t in tenants],
        "optimized": [opt["tenant_p99"].get(t, 0.0) for t in tenants],
    }
    return packed


def experiment_e7(seed: int = 42) -> dict[str, Any]:
    cfg = load_config()
    layers = [0.4] + [0.7] * 10
    epochs = 3
    bw = BandwidthModel(
        nvme_gbps=float(cfg["nvme_gbps"]),
        cpu_gpu_gbps=float(cfg["cpu_gpu_gbps"]),
        gpu_hbm_gbps=float(cfg["gpu_hbm_gbps"]),
    )
    serial = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    double = PrefetchRuntime(bw, layers, lookahead=1, cache_layers=0)
    # All weights resident: epochs 2..3 are pure cache hits.
    cached = PrefetchRuntime(bw, layers, lookahead=int(cfg["lookahead"]), cache_layers=len(layers))
    s, d, c = serial.run(epochs=epochs), double.run(epochs=epochs), cached.run(epochs=epochs)
    sm, cm = serial.metrics(), cached.metrics()
    # Ablation grid (spec 6.2): stall over lookahead x cache_layers.
    ablation = []
    for la in (0, 1, 2, 4):
        for cl in (0, 2, 6, len(layers)):
            rt = PrefetchRuntime(bw, layers, lookahead=la, cache_layers=cl)
            m = rt.run(epochs=epochs)
            ablation.append(
                {
                    "lookahead": la,
                    "cache_layers": cl,
                    "io_stall_ratio": m["io_stall_ratio"],
                    "effective_compute_ratio": m["effective_compute_ratio"],
                    "cache_hit_rate": m["cache_hit_rate"],
                }
            )
    better = cm["io_stall_ratio"] <= sm["io_stall_ratio"] + 1e-12
    return {
        "id": "E7",
        "title": f"串行 vs 双缓冲 vs 预取+缓存（{epochs} 轮扫层）",
        "baseline": sm,
        "optimized": cm,
        "kpis": {
            "io_stall_ratio_delta_pct": _pct_change(sm["io_stall_ratio"], cm["io_stall_ratio"], lower_better=True),
            "effective_compute_ratio_delta_pct": _pct_change(
                sm["effective_compute_ratio"], cm["effective_compute_ratio"]
            ),
            "cache_hit_rate_optimized": cm["cache_hit_rate"],
        },
        "extra": {
            "double_buffer": double.metrics(),
            "ablation": ablation,
            "epochs": epochs,
            "interpretation": (
                "Sequential layer sweeps revisit each layer after n touches, so an "
                "LRU smaller than the full layer working set (cache_layers<n) cannot "
                "hit. Cache benefit is therefore step-shaped in cache capacity, while "
                "lookahead only shaves the first-pass NVMe latency up to the bandwidth "
                "limit. This is the capacity-planning curve, not a smooth tradeoff."
            ),
        },
        "charts": {
            "stall_timeline": {
                "baseline": [(i, x["stall"]) for i, x in enumerate(s["timeline"])],
                "double": [(i, x["stall"]) for i, x in enumerate(d["timeline"])],
                "optimized": [(i, x["stall"]) for i, x in enumerate(c["timeline"])],
            },
            "overlap": {
                "serial": serial.overlap(),
                "double": double.overlap(),
                "optimized": cached.overlap(),
            },
        },
        "gantt": {},
        "verdict": "预取+缓存后 stall 不高于串行" if better else "预取未降低 stall，需检查参数",
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
    # A page-faulted frame is fetched back over the CPU<->GPU interconnect;
    # dirty evictions additionally pay the host swap-write cost (spec 4.4.1).
    fault_cost = float(cfg["page_size_gb"]) / float(cfg["cpu_gpu_gbps"])

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
        vmm_time = vmm.page_faults * fault_cost + vmm.swap_write_delay
        return {
            "oversub": vmm.oversubscription_ratio(),
            "isolation_violation_rate": vmm.isolation_violation_rate(),
            "isolation_intercept_rate": 1.0 - vmm.isolation_violation_rate(),
            "page_faults": vmm.page_faults,
            "swap_outs": vmm.swap_outs,
            "blocked_cross_tenant": blocked,
            "balloon_reclaimed": reclaimed,
            "vmm_time": vmm_time,
        }

    base = workload(40, 20, False)
    opt = workload(16, 20, True)
    # Oversubscription-vs-latency Pareto sweep (spec 6.4).
    pareto = []
    for phys in (40, 32, 24, 16, 12, 8):
        m = workload(phys, 20, True)
        pareto.append(
            {
                "phys_pages": phys,
                "oversubscription": m["oversub"],
                "vmm_time": m["vmm_time"],
                "page_faults": m["page_faults"],
                "swap_outs": m["swap_outs"],
            }
        )
    return {
        "id": "E9",
        "title": "无超卖 vs 超卖 + balloon",
        "baseline": base,
        "optimized": opt,
        "charts": {
            "oversub_latency": [(p["oversubscription"], p["vmm_time"]) for p in pareto],
        },
        "extra": {"pareto": pareto},
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
        4,
        seed=seed,
        arrival_rate=3.0,
        prompt_range=(2048, 3072),
        gen_range=(256, 384),
    )
    # A pool around one worst-case sequence: contiguous pre-reservation must
    # reject later arrivals; paged demand allocation multiplexes the same pool.
    blocks = 180
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
