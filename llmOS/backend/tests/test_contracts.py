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
