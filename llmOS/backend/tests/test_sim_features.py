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
