from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional

from llmos.errors import AllocError
from llmos.sim.clock import DiscreteEventSim
from llmos.sim.metrics import MetricsBus
from llmos.task1_kv_memory.page_table import PageTable
from llmos.task2_scheduler.request import Request, ReqState
from llmos.task3_runtime.prefetch_runtime import PrefetchRuntime
from llmos.task3_runtime.quota_manager import QuotaManager
from llmos.task4_gpu_virt.mps_scheduler import MPSScheduler


class Strategy(Enum):
    FCFS = "fcfs"
    SJF = "sjf"
    MLFQ = "mlfq"
    PREEMPTIVE = "preemptive"
    FAIR = "fair"


class BaseScheduler(ABC):
    def __init__(
        self,
        sim: DiscreteEventSim,
        batch_size: int,
        step_cost: float,
        page_table: PageTable,
        metrics: MetricsBus,
        alpha_prefill: float = 2e-5,
        beta_decode: float = 1e-3,
        quota: Optional[QuotaManager] = None,
        mps: Optional[MPSScheduler] = None,
        hunger_threshold: float = 0.05,
        time_slice: float = 0.01,
        prefetch: Optional[PrefetchRuntime] = None,
    ) -> None:
        self.sim = sim
        self.batch_size = batch_size
        self.step_cost = step_cost
        self.page_table = page_table
        self.metrics = metrics
        self.alpha_prefill = alpha_prefill
        self.beta_decode = beta_decode
        self.quota = quota
        self.mps = mps
        self.prefetch = prefetch
        self.hunger_threshold = hunger_threshold
        self.time_slice = time_slice
        self.waiting: list[Request] = []
        self.running: list[Request] = []
        self.finished: list[Request] = []
        self.failed: list[Request] = []
        self.busy_time = 0.0
        self.idle_tick = step_cost
        self._scheduled = False
        self.gantt: list[dict] = []
        self.tokens: dict[str, float] = {}
        self.pipeline_log: list[str] = []
        self.decode_ticks = 0
        self.peak_kv_util = 0.0
        self.peak_kv_frag = 0.0
        self.peak_concurrency = 0
        self.peak_live_seqs = 0
        self.ever_hungry: set[int] = set()
        self.ctx_switch_time = 0.0
        self.arrivals = 0

    def submit(self, req: Request) -> None:
        req.state = ReqState.WAITING
        req.last_wait_start = self.sim.now
        self.waiting.append(req)
        self.arrivals += 1
        self.metrics.inc("arrivals")
        if not self._scheduled:
            self._scheduled = True
            self.sim.schedule(0.0, self._decode_tick, kind="DecodeTick")

    def _quota_ok(self, req: Request) -> bool:
        if self.quota is None:
            return True
        return self.quota.allow_slice(req.tenant, self.step_cost)

    def _confirm_slice(self, batch: List[Request]) -> tuple[List[Request], float]:
        """Quota + MPS decide which tenants may run this DecodeTick."""
        extra = 0.0
        owner: Optional[str] = None
        present = {r.tenant for r in batch}
        if self.mps and present:
            ranked = []
            for name in present:
                t = self.mps.tenants.get(name)
                share = t.share if t is not None else 1.0
                acc = t.accumulated if t is not None else 0.0
                ranked.append((acc / max(share, 1e-9), name))
            owner = min(ranked)[1]
            extra = self.mps.run_slice(owner, self.sim.now)
        admitted: list[Request] = []
        for req in batch:
            if owner is not None and req.tenant != owner:
                continue
            if not self._quota_ok(req):
                continue
            admitted.append(req)
        if not admitted:
            admitted = [r for r in batch if self._quota_ok(r)]
        return admitted, extra

    def _sort_waiting(self, strategy: Strategy) -> None:
        if strategy == Strategy.SJF:
            self.waiting.sort(key=lambda r: (r.remaining, r.arrival_time))
        elif strategy == Strategy.PREEMPTIVE:
            self.waiting.sort(key=lambda r: (-r.priority, r.arrival_time))
        elif strategy == Strategy.FAIR:
            for r in self.waiting:
                self.tokens.setdefault(r.tenant, 1.0)
            self.waiting.sort(key=lambda r: (-self.tokens.get(r.tenant, 1.0), r.arrival_time))
        else:
            self.waiting.sort(key=lambda r: r.arrival_time)

    def _reap_finished(self) -> None:
        still: list[Request] = []
        for req in self.running:
            if req.state == ReqState.FINISHED:
                self.page_table.free(req.seq_id)
                self.finished.append(req)
                self.metrics.inc("completed")
                self.metrics.observe("latency", req.finish_time - req.arrival_time)
                if req.first_token_time >= 0:
                    self.metrics.observe("ttft", req.first_token_time - req.arrival_time)
            else:
                still.append(req)
        self.running = still

    def _iteration_cost(self, batch: List[Request]) -> float:
        prefill = sum(self.alpha_prefill * r.prompt_len for r in batch if r.generated == 0)
        decode = self.beta_decode * max(1, len(batch))
        return max(self.step_cost, prefill + decode)

    def _fail(self, req: Request) -> None:
        req.state = ReqState.FINISHED
        req.finish_time = self.sim.now
        self.failed.append(req)
        self.metrics.inc("oom")
        if req.seq_id >= 0:
            self.page_table.free(req.seq_id)

    def _ensure_seq(self, req: Request) -> bool:
        if req.seq_id >= 0:
            return True
        try:
            if self.quota and not self.quota.alloc_mem(req.tenant, 0.01):
                self._fail(req)
                return False
            req.seq_id = self.page_table.new_sequence(req.max_tokens + req.prompt_len)
            for _ in range(req.prompt_len):
                self.page_table.append_token(req.seq_id)
            return True
        except AllocError:
            self._fail(req)
            return False

    @abstractmethod
    def _pick_batch(self) -> List[Request]:
        ...

    def _maybe_schedule(self) -> None:
        self._decode_tick()

    def _decode_tick(self) -> None:
        """Standard decode iteration. Prefill batches use the same skeleton as PrefillTick."""
        self._reap_finished()
        batch = self._pick_batch()
        if not batch:
            if self.waiting or self.running:
                self.sim.schedule(self.idle_tick, self._decode_tick, kind="DecodeTick")
            else:
                self._scheduled = False
            return

        self.decode_ticks += 1
        admitted, mps_extra = self._confirm_slice(batch)
        self.ctx_switch_time += mps_extra
        now = self.sim.now
        for r in self.waiting:
            wait = now - (r.last_wait_start if r.last_wait_start >= 0 else r.arrival_time)
            if wait > self.hunger_threshold:
                self.ever_hungry.add(r.rid)
        stall = 0.0
        if self.prefetch is not None:
            stall = self.prefetch.tick(self.sim)
        kind = "PrefillTick" if any(r.generated == 0 for r in (admitted or batch)) else "DecodeTick"
        self.pipeline_log.append(kind)
        work = admitted if admitted else batch
        cost = self._iteration_cost(work) + stall + mps_extra
        self.gantt.append(
            {
                "t": self.sim.now,
                "kind": kind,
                "rids": [r.rid for r in admitted],
                "tenants": [r.tenant for r in admitted],
                "mps_owner": admitted[0].tenant if admitted else None,
                "stall": stall,
            }
        )

        self.page_table.allocator.now = self.sim.now
        for req in admitted:
            try:
                self.page_table.allocator.tenant = req.tenant
                if not self._ensure_seq(req):
                    continue
                req.step(self.sim.now)
                self.page_table.append_token(req.seq_id)
                if req.tenant in self.tokens:
                    self.tokens[req.tenant] = max(0.0, self.tokens[req.tenant] - 1.0)
            except AllocError:
                self._fail(req)

        self.busy_time += cost
        util = self.page_table.allocator.utilization()
        frag = self.page_table.allocator.fragmentation_ratio()
        self.peak_kv_util = max(self.peak_kv_util, util)
        self.peak_kv_frag = max(self.peak_kv_frag, frag)
        self.peak_concurrency = max(self.peak_concurrency, len(self.running))
        self.peak_live_seqs = max(self.peak_live_seqs, len(self.page_table.seqs))
        self.metrics.gauge("kv_util", self.sim.now, util)
        self.metrics.gauge("kv_frag", self.sim.now, frag)
        self._reap_finished()
        self._pick_batch()
        nxt = "PrefillTick" if any(r.generated == 0 for r in self.running) else "DecodeTick"
        if self.waiting or self.running:
            self.sim.schedule(max(cost, self.idle_tick), self._decode_tick, kind=nxt)
        else:
            self._scheduled = False

    def metrics_dict(self) -> Dict[str, Any]:
        horizon = max(self.sim.now, 1e-9)
        hung = len(self.ever_hungry)
        decided = len(self.finished) + len(self.failed)
        total = decided + len(self.waiting) + len(self.running)
        by_tenant: dict[str, list[float]] = {}
        for r in self.finished:
            if r.finish_time >= 0:
                by_tenant.setdefault(r.tenant, []).append(r.finish_time - r.arrival_time)
        tenant_p99 = {}
        for k, xs in by_tenant.items():
            xs = sorted(xs)
            tenant_p99[k] = xs[min(len(xs) - 1, max(0, int(round(0.99 * (len(xs) - 1)))))]
        p99s = list(tenant_p99.values())
        mean = sum(p99s) / len(p99s) if p99s else 0.0
        var = sum((x - mean) ** 2 for x in p99s) / len(p99s) if p99s else 0.0
        vmm = self.page_table.allocator.vmm
        return {
            "throughput": len(self.finished) / horizon,
            "completed": float(len(self.finished)),
            "oom": float(len(self.failed)),
            "oom_ratio": len(self.failed) / max(1, decided),
            "gpu_busy_ratio": self.busy_time / horizon,
            "hunger_ratio": hung / max(1, self.arrivals or total),
            "ttft_p50": self.metrics.percentile("ttft", 50),
            "ttft_p99": self.metrics.percentile("ttft", 99),
            "latency_p50": self.metrics.percentile("latency", 50),
            "latency_p99": self.metrics.percentile("latency", 99),
            "kv_utilization": self.page_table.allocator.utilization(),
            "kv_utilization_peak": self.peak_kv_util,
            "kv_fragmentation": self.page_table.allocator.fragmentation_ratio(),
            "kv_fragmentation_peak": self.peak_kv_frag,
            "peak_concurrency": float(self.peak_concurrency),
            "peak_live_seqs": float(self.peak_live_seqs),
            "kv_evictions": float(self.page_table.allocator.evictions),
            "ctx_switch_overhead_ratio": self.ctx_switch_time / horizon,
            "ctx_switch_count": float(self.mps.switch_count if self.mps else 0),
            "tenant_p99": tenant_p99,
            "tenant_p99_var": var,
            "vmm_oversub": vmm.oversubscription_ratio() if vmm else 1.0,
            "vmm_swap_outs": float(vmm.swap_outs) if vmm else 0.0,
            "isolation_violation_rate": vmm.isolation_violation_rate() if vmm else 0.0,
            "kv_page_faults": float(self.page_table.allocator.page_faults),
            "decode_ticks": float(self.decode_ticks),
            "io_stall_ratio": self.prefetch.io_stall_ratio() if self.prefetch else 0.0,
        }


class StaticBatchingScheduler(BaseScheduler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._batch_active: list[Request] = []

    def _pick_batch(self) -> List[Request]:
        if self._batch_active:
            if any(r.state != ReqState.FINISHED for r in self._batch_active):
                return [r for r in self._batch_active if r.state != ReqState.FINISHED]
            self._batch_active = []
        if len(self.waiting) < self.batch_size and self.running:
            return list(self.running)
        if len(self.waiting) < self.batch_size:
            return []
        self._sort_waiting(Strategy.FCFS)
        chosen = self.waiting[: self.batch_size]
        self.waiting = self.waiting[self.batch_size :]
        self.running.extend(chosen)
        self._batch_active = list(chosen)
        return chosen


class ContinuousBatchingScheduler(BaseScheduler):
    def __init__(self, *a, strategy: Strategy = Strategy.FCFS, **kw) -> None:
        super().__init__(*a, **kw)
        self.strategy = strategy

    def _pick_batch(self) -> List[Request]:
        self._sort_waiting(self.strategy)
        if self.strategy == Strategy.PREEMPTIVE and self.running:
            self.running.sort(key=lambda r: r.priority)
            while self.waiting and self.running and self.waiting[0].priority > self.running[0].priority:
                victim = self.running.pop(0)
                victim.state = ReqState.PREEMPTED
                victim.last_wait_start = self.sim.now
                self.waiting.append(victim)
                self._sort_waiting(self.strategy)
        slots = self.batch_size - len(self.running)
        admitted: list[Request] = []
        still_wait: list[Request] = []
        for req in self.waiting:
            if slots <= 0:
                still_wait.append(req)
                continue
            if not self._quota_ok(req):
                still_wait.append(req)
                continue
            if self.strategy == Strategy.FAIR:
                self.tokens[req.tenant] = self.tokens.get(req.tenant, 1.0) + 0.15
            admitted.append(req)
            slots -= 1
        self.waiting = still_wait
        self.running.extend(admitted)
        return list(self.running)
