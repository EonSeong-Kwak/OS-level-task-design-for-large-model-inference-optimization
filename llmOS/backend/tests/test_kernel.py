from llmos.errors import AllocError
from llmos.sim.clock import DiscreteEventSim
from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator
from llmos.task1_kv_memory.page_table import PageTable
from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
from llmos.task3_runtime.quota_manager import QuotaManager, TenantQuota
from llmos.task3_runtime.storage_model import BandwidthModel
from llmos.task4_gpu_virt.gpu_vmm import GPUVMM
from llmos.errors import IsolationError
from llmos.experiments import run_experiment


def test_clock_order():
    sim = DiscreteEventSim()
    order = []
    sim.schedule(2, lambda: order.append("b"))
    sim.schedule(1, lambda: order.append("a"))
    sim.run(10)
    assert order == ["a", "b"]
    assert sim.now == 10


def test_page_table_translate_and_demand_paging():
    alloc = BlockAllocator(8, 4)
    pt = PageTable(alloc)
    sid = pt.new_sequence()
    for _ in range(5):
        pt.append_token(sid)
    assert pt.token_len[sid] == 5
    bid, off = pt.translate(sid, 4)
    assert off == 0
    assert len(pt.seqs[sid]) == 2


def test_translate_rejects_oob():
    alloc = BlockAllocator(4, 4)
    pt = PageTable(alloc)
    sid = pt.new_sequence()
    pt.append_token(sid)
    try:
        pt.translate(sid, 3)
        assert False
    except IndexError:
        pass


def test_cow_shares_then_splits():
    alloc = BlockAllocator(16, 4)
    pt = PageTable(alloc)
    src = pt.new_sequence()
    for _ in range(4):
        pt.append_token(src)
    dst = pt.fork(src)
    assert pt.seqs[src] == pt.seqs[dst]
    assert alloc.blocks[pt.seqs[src][0]].ref_count == 2
    pt.append_token(dst)
    assert pt.seqs[src][0] == pt.seqs[dst][0]  # prefix still shared
    # writing a new block should not free prefix
    assert alloc.blocks[pt.seqs[src][0]].ref_count >= 2


def test_free_only_when_refcount_zero():
    alloc = BlockAllocator(4, 4)
    ids = alloc.allocate(1)
    alloc.fork(ids[0])
    alloc.free(ids)
    assert ids[0] not in alloc._free
    alloc.free(ids)
    assert ids[0] in alloc._free


def test_alloc_error_when_exhausted():
    alloc = BlockAllocator(1, 4)
    alloc.allocate(1)
    # refcount 1 resident cannot steal another while still referenced? steal requires ref_count==1
    # steal WILL evict the only block
    stolen = alloc.steal_for_eviction(1.0)
    assert stolen is not None
    try:
        alloc.allocate(2)
        assert False
    except AllocError:
        pass


def test_quota_hard_limit():
    qm = QuotaManager(4)
    qm.register(TenantQuota("t", gpu_mem_gb=1.0))
    assert qm.alloc_mem("t", 0.8)
    assert not qm.alloc_mem("t", 0.5)
    assert qm.is_isolated("t")


def test_prefetch_overlap_beats_serial():
    layers = [0.5] * 8
    bw = BandwidthModel()
    serial = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    overlap = PrefetchRuntime(bw, layers, lookahead=2, cache_layers=3)
    serial.run()
    overlap.run()
    assert overlap.io_stall_ratio() <= serial.io_stall_ratio() + 1e-9


def test_vmm_isolation_and_oversub():
    vmm = GPUVMM(phys_pages=4)
    vmm.create_tenant("a", 6)
    for i in range(6):
        vmm.access("a", i, True, float(i))
    assert vmm.oversubscription_ratio() == 6 / 4
    try:
        vmm.access("a", 99, False, 1.0)
        assert False
    except IsolationError:
        pass
    assert vmm.isolation_violation_rate() == 0.0


def test_experiments_have_baseline_and_optimized():
    for exp_id in ["E1", "E4", "E7", "E9"]:
        r = run_experiment(exp_id, seed=1)
        assert "baseline" in r and "optimized" in r
        assert r["id"] == exp_id


def test_append_page_fault_restores_after_lru():
    vmm = GPUVMM(phys_pages=2)
    vmm.create_tenant("default", 8)
    alloc = BlockAllocator(4, 2, vmm=vmm)
    pt = PageTable(alloc)
    sid = pt.new_sequence()
    for _ in range(4):
        pt.append_token(sid)
    stolen = alloc.steal_for_eviction(1.0)
    assert stolen is not None
    before = alloc.page_faults
    pt.append_token(sid)
    assert pt.token_len[sid] == 5
    assert alloc.page_faults >= before


def test_decode_tick_pipeline_runs_end_to_end():
    from llmos.experiments import run_serving
    from llmos.workloads.synthetic import synthetic_poisson

    reqs = synthetic_poisson(
        4, seed=1, arrival_rate=80.0, prompt_range=(8, 12), gen_range=(3, 5)
    )
    out = run_serving(reqs, strategy="fcfs")
    assert out["decode_ticks"] > 0
    assert out["completed"] >= 1
    assert out["gantt"]
    assert out["gantt"][0]["kind"] in {"PrefillTick", "DecodeTick"}
    kinds = {row["kind"] for row in out["gantt"]}
    assert "DecodeTick" in kinds or "PrefillTick" in kinds
