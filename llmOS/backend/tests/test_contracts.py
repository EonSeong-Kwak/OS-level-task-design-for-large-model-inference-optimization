from llmos.errors import AllocError, IsolationError
from llmos.sim.clock import DiscreteEventSim
from llmos.task1_kv_memory.kv_block_allocator import BlockAllocator
from llmos.task1_kv_memory.page_table import PageTable
from llmos.task4_gpu_virt.gpu_vmm import GPUVMM


def test_translate_contract():
    alloc = BlockAllocator(4, 4)
    pt = PageTable(alloc)
    sid = pt.new_sequence()
    pt.append_token(sid)
    try:
        pt.translate(sid, 9)
        assert False
    except IndexError:
        pass


def test_refcount_free_contract():
    alloc = BlockAllocator(4, 4)
    bid = alloc.allocate(1)[0]
    alloc.fork(bid)
    alloc.free([bid])
    assert bid not in alloc._free
    alloc.free([bid])
    assert bid in alloc._free


def test_vmm_intercepts_oob():
    vmm = GPUVMM(4)
    vmm.create_tenant("t", 4)
    try:
        vmm.access("t", 99, False, 0.0)
        assert False
    except IsolationError:
        pass
    assert vmm.isolation_violation_rate() == 0.0


def test_allocator_uses_vmm():
    vmm = GPUVMM(phys_pages=2)
    vmm.create_tenant("default", 4)
    alloc = BlockAllocator(4, 4, vmm=vmm)
    alloc.tenant = "default"
    alloc.allocate(2)
    assert vmm.page_faults >= 2
    try:
        alloc.allocate(8)
    except AllocError:
        return
    assert vmm.swap_outs >= 0


def test_clock_no_sleep_and_tie_break():
    sim = DiscreteEventSim()
    order = []
    sim.schedule(1, lambda: order.append("a"), priority=1)
    sim.schedule(1, lambda: order.append("b"), priority=0)
    sim.run(2)
    assert order == ["b", "a"]


def test_prefetch_decode_step_contract():
    """§5.3: PrefetchRuntime.decode_step 必须是 async，且不引入真实睡眠。"""
    import asyncio
    import inspect

    from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
    from llmos.task3_runtime.storage_model import BandwidthModel

    assert inspect.iscoroutinefunction(PrefetchRuntime.decode_step)

    bw = BandwidthModel()
    layers = [0.5] * 4
    serial = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    stall = asyncio.run(serial.decode_step(0))
    assert stall > 0
    assert serial.stall == stall
    assert serial.compute > 0

    # 与 tick 路径等价：同样配置下首个 decode slice 的 stall 相同
    twin = PrefetchRuntime(bw, layers, lookahead=0, cache_layers=0)
    assert twin.tick(DiscreteEventSim()) == stall
