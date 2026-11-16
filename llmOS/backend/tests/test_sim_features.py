"""Automated simulation cases for the internship functional goals."""

from pathlib import Path

from llmos.errors import AllocError, IsolationError
from llmos.experiments import run_experiment, run_serving
from llmos.sim.clock import DiscreteEventSim
from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator
from llmos.task1_kv_memory.page_table import PageTable
from llmos.task2_scheduler.request import Request
from llmos.task2_scheduler.scheduler import Strategy
from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
from llmos.task3_runtime.quota_manager import QuotaManager, TenantQuota
from llmos.task3_runtime.storage_model import BandwidthModel
from llmos.task4_gpu_virt.gpu_vmm import GPUVMM
from llmos.task4_gpu_virt.mps_scheduler import MPSScheduler, TenantCompute


def _reqs(*specs: tuple) -> list[Request]:
    out = []
    for i, spec in enumerate(specs, start=1):
        arrival, prompt, gen, tenant, prio = spec
        out.append(
            Request(
                rid=i,
                arrival_time=arrival,
                prompt_len=prompt,
                max_tokens=gen,
                tenant=tenant,
                priority=prio,
            )
        )
    return out


def test_kv_block_allocate_translate_demand_page():
    alloc = BlockAllocator(8, 4)
    pt = PageTable(alloc)
    sid = pt.new_sequence()
    for _ in range(9):
        pt.append_token(sid)
    assert pt.token_len[sid] == 9
    assert len(pt.seqs[sid]) == 3
    bid, off = pt.translate(sid, 0)
    assert off == 0 and bid == pt.seqs[sid][0]
    bid2, off2 = pt.translate(sid, 8)
    assert off2 == 0 and bid2 == pt.seqs[sid][2]


def test_kv_cow_and_lru_eviction():
    alloc = BlockAllocator(16, 4)
    pt = PageTable(alloc)
    src = pt.new_sequence()
    for _ in range(4):
        pt.append_token(src)
    dst = pt.fork(src)
    assert alloc.blocks[pt.seqs[src][0]].ref_count == 2
    pt.append_token(dst)
    assert pt.seqs[src][0] == pt.seqs[dst][0]

    small = BlockAllocator(2, 2)
    pt2 = PageTable(small)
    sid = pt2.new_sequence()
    for _ in range(4):
        pt2.append_token(sid)
    before = small.evictions
    pt2.append_token(sid)
    assert small.evictions > before


def test_contiguous_prealloc_vs_paged():
    paged = PageTable(BlockAllocator(16, 4))
    cont = PageTable(BlockAllocator(16, 4))
    cont.mode = "contiguous"
    s1 = paged.new_sequence()
    s2 = cont.new_sequence(max_tokens=16)
    for _ in range(5):
        paged.append_token(s1)
        cont.append_token(s2)
    assert len(paged.seqs[s1]) == 2
    assert len(cont.seqs[s2]) == 4


def test_static_vs_continuous_batching():
    from llmos.config import load_config

    cfg = load_config()
    cfg["batch_size"] = 2
    reqs = _reqs(
        (0.0, 4, 4, "alpha", 0),
        (0.0, 4, 4, "beta", 0),
        (0.0, 4, 4, "alpha", 0),
        (0.0, 4, 4, "beta", 0),
    )
    static = run_serving(reqs, cfg, mode="static", strategy="fcfs")
    cont = run_serving(reqs, cfg, mode="continuous", strategy="fcfs")
    assert cont["completed"] >= 1
    assert static["completed"] >= 1
    assert cont["throughput"] >= static["throughput"] - 1e-9


def test_fcfs_sjf_preemptive_fair_all_run():
    reqs = _reqs(
        (0.0, 8, 6, "alpha", 0),
        (0.0, 4, 3, "beta", 2),
        (0.01, 6, 5, "alpha", 1),
        (0.01, 4, 3, "beta", 0),
    )
    out = {}
    for name in ("fcfs", "sjf", "preemptive", "fair"):
        r = run_serving(reqs, strategy=name)
        out[name] = r
        assert r["decode_ticks"] > 0
        assert r["completed"] + r["oom"] >= 1
        kinds = {row["kind"] for row in r["gantt"]}
        assert kinds & {"PrefillTick", "DecodeTick"}
    assert Strategy.FCFS.value == "fcfs"


def test_prefill_and_decode_ticks_are_distinct():
    reqs = _reqs((0.0, 6, 8, "alpha", 0), (0.0, 6, 8, "beta", 0))
    r = run_serving(reqs, strategy="fcfs")
    kinds = [row["kind"] for row in r["gantt"]]
    assert "PrefillTick" in kinds
    assert "DecodeTick" in kinds
    first_decode = next(i for i, k in enumerate(kinds) if k == "DecodeTick")
    assert any(k == "PrefillTick" for k in kinds[: first_decode + 1])


def test_decode_tick_pipeline_fields():
    reqs = _reqs((0.0, 4, 4, "alpha", 0), (0.0, 4, 4, "beta", 0))
    r = run_serving(reqs, strategy="fcfs")
    row = r["gantt"][0]
    assert "kind" in row and "stall" in row and "tenants" in row and "mps_owner" in row
    assert r["io_stall_ratio"] >= 0.0
    assert r["kv_page_faults"] >= 0.0


def test_prefetch_overlap_and_does_not_touch_page_table():
    layers = [0.4] * 6
    bw = BandwidthModel()
    serial = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    overlap = PrefetchRuntime(bw, layers, lookahead=2, cache_layers=2)
    s = serial.run()
    o = overlap.run()
    assert o["io_stall_ratio"] <= s["io_stall_ratio"] + 1e-9
    assert not hasattr(overlap, "seqs")
    sim = DiscreteEventSim()
    overlap.reset()
    stall = overlap.tick(sim)
    assert stall >= 0.0
    assert "DmaComplete" in {getattr(e, "kind", "") for e in sim._heap} or sim.last_kind in {"", "DmaComplete"}


def test_quota_share_and_hard_mem_cap():
    qm = QuotaManager(total_gpu_mem_gb=2.0)
    qm.register(TenantQuota("alpha", gpu_compute_share=1.0, gpu_mem_gb=0.5))
    qm.register(TenantQuota("zero", gpu_compute_share=0.0, gpu_mem_gb=1.0))
    assert qm.alloc_mem("alpha", 0.4)
    assert not qm.alloc_mem("alpha", 0.2)
    assert qm.allow_slice("alpha", 0.01)
    assert not qm.allow_slice("zero", 0.01)
    tight = run_serving(
        _reqs((0.0, 8, 4, "alpha", 0), (0.0, 8, 4, "beta", 0)),
        mem_cap=0.02,
    )
    loose = run_serving(
        _reqs((0.0, 8, 4, "alpha", 0), (0.0, 8, 4, "beta", 0)),
        mem_cap=8.0,
    )
    assert tight["oom"] >= loose["oom"]


def test_vmm_vas_oversub_balloon_isolation():
    vmm = GPUVMM(phys_pages=4, page_size_gb=0.25)
    vmm.create_tenant("alpha", 8)
    vmm.create_tenant("beta", 8)
    for vpn in range(6):
        vmm.access("alpha", vpn, True, float(vpn))
    assert vmm.oversubscription_ratio() == 16 / 4
    assert vmm.swap_outs >= 1
    reclaimed = vmm.balloon_reclaim("alpha", 2)
    assert reclaimed >= 1
    try:
        vmm.access("alpha", 99, False, 1.0)
        assert False
    except IsolationError:
        pass
    assert vmm.isolation_violation_rate() == 0.0


def test_mps_timeslice_share_3_to_1():
    even = MPSScheduler(time_slice=0.01)
    even.register(TenantCompute("alpha", share=1.0))
    even.register(TenantCompute("beta", share=1.0))
    skewed = MPSScheduler(time_slice=0.01)
    skewed.register(TenantCompute("alpha", share=3.0))
    skewed.register(TenantCompute("beta", share=1.0))
    now = 0.0
    for _ in range(40):
        even.run_slice(even.pick_next(now), now)
        skewed.run_slice(skewed.pick_next(now), now)
        now += 0.01
    assert skewed.tenants["alpha"].accumulated > skewed.tenants["beta"].accumulated
    assert abs(even.tenants["alpha"].accumulated - even.tenants["beta"].accumulated) < 1e-9


def test_scheduler_does_not_import_vmm_or_bandwidth():
    src = (
        Path(__file__).resolve().parents[1]
        / "llmos"
        / "task2_scheduler"
        / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "GPUVMM" not in src
    assert "BandwidthModel" not in src


def test_all_experiments_e1_to_e13_execute():
    for exp_id in [f"E{i}" for i in range(1, 14)]:
        r = run_experiment(exp_id, seed=2)
        assert r["id"] == exp_id
        assert "baseline" in r and "optimized" in r
        assert r.get("config_digest")


def test_missing_kpis_are_now_reported():
    e1 = run_experiment("E1", seed=2)
    assert "kv_utilization_peak" in e1["optimized"]
    assert "peak_concurrency" in e1["optimized"]
    assert "oom_ratio" in e1["optimized"]
    assert "util_lift_pct" in e1["extra"]
    assert "frag_drop_pct" in e1["extra"]
    e7 = run_experiment("E7", seed=2)
    assert "io_stall_ratio_delta_pct" in e7["kpis"]
    e10 = run_experiment("E10", seed=2)
    assert "ctx_switch_overhead_ratio" in e10["optimized"]
    e11 = run_experiment("E11", seed=2)
    assert "oom_ratio" in e11["baseline"]
    e12 = run_experiment("E12", seed=2)
    assert e12["optimized"]["max_model_gb"] >= e12["baseline"]["max_model_gb"]
    e13 = run_experiment("E13", seed=2)
    assert e13["extra"]["optimized_tenants"] == 8


# ---------------------------------------------------------------------------
# Spec-alignment additions: real long-context pressure, FAIR share source,
# MLFQ demotion, block-granular quotas, cache quota, VMM pin/dirty swap,
# multi-epoch prefetch cache hits, E9 Pareto sweep, and the trace loader.
# ---------------------------------------------------------------------------


def test_long_context_w2_is_spec_sized():
    from llmos.config import load_config
    from llmos.workloads.synthetic import from_cfg

    w2 = load_config()["w2"]
    assert tuple(w2["prompt_range"]) == (2048, 4096)
    assert tuple(w2["gen_range"]) == (256, 512)
    reqs = from_cfg(load_config(), "w2", seed=42)
    assert reqs[0].prompt_len >= 2048


def test_contiguous_pool_exhaustion_rejects_without_eviction():
    reqs = _reqs(
        (0.0, 256, 64, "alpha", 0),
        (0.001, 256, 64, "beta", 0),
    )
    cont = run_serving(reqs, kv_mode="contiguous", num_blocks=24, phys_pages=24)
    paged = run_serving(reqs, kv_mode="paged", num_blocks=24, phys_pages=24)
    assert cont["oom"] >= 1
    assert cont["kv_evictions"] == 0
    assert paged["completed"] >= cont["completed"]
    assert paged["oom"] <= cont["oom"]


def test_e1_paged_reduces_oom_under_w2_pressure():
    e1 = run_experiment("E1", seed=42)
    assert e1["optimized"]["oom_ratio"] < e1["baseline"]["oom_ratio"]


def test_fair_lowers_tenant_variance_under_burst():
    e6 = run_experiment("E6", seed=42)
    assert e6["optimized"]["tenant_p99_var"] < e6["baseline"]["tenant_p99_var"]
    assert "tenant_p99" in e6["charts"]


def test_mlfq_demotes_long_running_request():
    from llmos.experiments import _drive
    from llmos.sim.clock import DiscreteEventSim
    from llmos.sim.metrics import MetricsBus
    from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator
    from llmos.task1_kv_memory.page_table import PageTable
    from llmos.task2_scheduler.scheduler import ContinuousBatchingScheduler

    sim = DiscreteEventSim()
    pt = PageTable(BlockAllocator(64, 16))
    sched = ContinuousBatchingScheduler(
        sim=sim,
        batch_size=4,
        step_cost=0.001,
        page_table=pt,
        metrics=MetricsBus(),
        strategy=Strategy.MLFQ,
    )
    long_req = Request(rid=1, arrival_time=0.0, prompt_len=8, max_tokens=80, tenant="alpha", priority=0)
    short_req = Request(rid=2, arrival_time=0.02, prompt_len=8, max_tokens=4, tenant="beta", priority=0)
    sched.submit(long_req)
    sched.submit(short_req)
    _drive(sched, [])
    # The 80-token request burns the q0 (2 ticks) and q1 (4 ticks) budgets.
    assert long_req.mlfq_level >= 1
    assert long_req.state.name == "FINISHED"


def test_quota_reservation_is_block_granular_and_released():
    reqs = _reqs((0.0, 100, 52, "alpha", 0))
    r = run_serving(reqs, mem_cap=0.04)  # ceil(152/16)=10 blocks * 0.005 = 0.05 > 0.04
    assert r["oom"] == 1
    loose = run_serving(reqs, mem_cap=8.0)
    assert loose["completed"] == 1


def test_weight_cache_is_charged_against_quota():
    from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
    from llmos.task3_runtime.quota_manager import QuotaManager, TenantQuota

    qm = QuotaManager(total_gpu_mem_gb=1.0)
    qm.register(TenantQuota("__weights__", gpu_compute_share=0.0, gpu_mem_gb=0.05))
    rt = PrefetchRuntime(
        BandwidthModel(),
        [0.02] * 6,
        lookahead=1,
        cache_layers=4,
        quota=qm,
    )
    m = rt.run(epochs=2)
    used = qm.used["__weights__"]
    assert used <= 0.05 + 1e-9
    assert m["cache_hits"] > 0


def test_pinned_vmm_frames_cannot_be_evicted():
    vmm = GPUVMM(phys_pages=2, page_size_gb=0.25)
    vmm.create_tenant("alpha", 4)
    vmm.pin("alpha", 0, 0.0)
    vmm.pin("alpha", 1, 0.0)
    try:
        vmm.pin("alpha", 2, 0.0)
        assert False, "pinned frames must not be evictable"
    except IsolationError:
        pass
    vmm.release("alpha", 0)
    ppn = vmm.pin("alpha", 2, 0.0)
    assert ppn in (0, 1)


def test_dirty_frame_release_pays_swap_write():
    dirty = GPUVMM(phys_pages=1, page_size_gb=0.25)
    dirty.create_tenant("alpha", 2)
    dirty.access("alpha", 0, True, 0.0)
    dirty.release("alpha", 0)
    assert dirty.swap_write_delay > 0.0

    clean = GPUVMM(phys_pages=1, page_size_gb=0.25)
    clean.create_tenant("alpha", 2)
    clean.access("alpha", 0, False, 0.0)
    clean.release("alpha", 0)
    assert clean.swap_write_delay == 0.0


def test_e7_multi_epoch_cache_ablation():
    e7 = run_experiment("E7", seed=42)
    ablation = e7["extra"]["ablation"]
    assert len(ablation) == 16
    assert e7["optimized"]["cache_hit_rate"] > 0.5
    full_cache = [a for a in ablation if a["cache_layers"] == 11]
    no_cache = [a for a in ablation if a["cache_layers"] == 0]
    assert min(a["io_stall_ratio"] for a in full_cache) < max(
        a["io_stall_ratio"] for a in no_cache
    )


def test_e9_oversub_pareto_is_monotone():
    e9 = run_experiment("E9", seed=42)
    pareto = e9["extra"]["pareto"]
    oversubs = [p["oversubscription"] for p in pareto]
    times = [p["vmm_time"] for p in pareto]
    assert oversubs == sorted(oversubs)
    assert times == sorted(times)
    assert times[-1] > times[0]


def test_trace_loader_jsonl_aliases_and_sorting(tmp_path):
    from llmos.workloads.trace_loader import load_jsonl_trace

    p = tmp_path / "trace.jsonl"
    p.write_text(
        '{"t": 0.2, "prompt_len": 10, "output_tokens": 5}\n'
        '{"arrival_time": 0.1, "input_tokens": 20, "gen_tokens": 8, "priority": 2, "tenant": "beta"}\n',
        encoding="utf-8",
    )
    reqs = load_jsonl_trace(p)
    assert [r.arrival_time for r in reqs] == [0.1, 0.2]
    assert reqs[0].prompt_len == 20 and reqs[0].max_tokens == 8
    assert reqs[0].priority == 2 and reqs[0].tenant == "beta"
    assert reqs[1].tenant == "alpha"  # row 0 without tenant rotates to alpha
