# 贡献与代码审查规范

本文件对应技术方案 §11「实施要点（给实现者的硬约束）」，作为 PR 合并前的强制审查清单。
评审人必须在 PR 描述中逐条勾选；任一条不满足则不得合并到 `main`。

## 1. 合并前必查（11 条硬约束）

### 1.1 KV 分页与页表

- [ ] **translate 必须失败得干净**：`PageTable.translate` 对非法 `seq_id` 或越界 `logical_idx`
      必须抛异常，**不得返回随机块**。对应测试：`tests/test_contracts.py::test_translate_contract`、
      `tests/test_kernel.py::test_translate_rejects_oob`。
- [ ] **free 只在 ref_count 归零时回收**：引用计数未清零前不得把块放回空闲栈，防止 COW 场景下
      提前回收导致共享方读到已复用块。对应测试：`test_refcount_free_contract`、
      `test_free_only_when_refcount_zero`。
- [ ] **steal_for_eviction 不得驱逐共享块**：候选集必须过滤 `ref_count == 1`；需要驱逐
      `ref_count > 1` 的块时必须先分裂（copy-on-write），否则视为数据竞争缺陷。

### 1.2 调度

- [ ] **静态批与持续批共享同一 `Request.step` 与同一代价函数**（`_iteration_cost`），
      否则两者对比不公平，E4 结论作废。
- [ ] **FAIR 的令牌发放与 MPS 的份额必须来自同一份租户配置**
      （`QuotaManager` 的 `TenantQuota.gpu_compute_share`），禁止在调度器里再写一套份额。

### 1.3 运行时 I/O

- [ ] **stall 只统计「计算因数据未就绪而等待」的时间**，不得把 DMA 忙碌时间计入 GPU 有效算力；
      `io_stall_ratio = stall / (compute + stall)`，`effective_compute_ratio = compute / (compute + stall)`。

### 1.4 GPU 虚拟化

- [ ] **越界必须拦截且分母 > 0**：`GPUVMM.isolation_violation_rate()` 在故意越界测试中
      分母（越界尝试次数）必须大于 0，分子（未拦截次数）必须为 0。
      对应测试：`test_vmm_intercepts_oob`、`test_vmm_vas_oversub_balloon_isolation`。

### 1.5 评测脚本

- [ ] **`scripts/run_ab.py` 默认同时打印 baseline 与 optimized 两列**，缺任一列视为评测脚本不合格。

### 1.6 确定性与安全

- [ ] **离散事件不得依赖线程调度顺序**；内核禁止 `time.sleep` / `asyncio.sleep`，
      asyncio 路径必须把完成时间写入事件队列。
- [ ] **所有随机只来自 `random.Random(seed)`**，禁止使用全局 `random` 状态。
- [ ] **禁止提交真实 API Key、云端账号、未脱敏日志**；数据库口令只走环境变量
      （`MYSQL_USER` / `MYSQL_PASSWORD`）。

## 2. 质量门禁

每次 PR 必须在本地与 CI 跑通：

```bash
cd backend
python -m pytest -q --cov=llmos --cov-fail-under=70
```

- 覆盖率 **≥ 70%**（当前实测 93.38%，预留回退空间）。
- **黄金 trace 回归**：`tests/test_golden_trace.py` 固定 `seed=42` 的 W1 子集，
  指标波动 > 1% 视为回归，必须查明原因后才能合并。
- 结果文件只由 `scripts/run_ab.py` 生成，评测报告禁止手抄数字。

## 3. 评审分工（角色 B / C 交叉审查）

| 改动范围 | 必须由谁评审 |
| --- | --- |
| `task1_kv_memory/`、`task3_runtime/quota_manager.py`、`task4_gpu_virt/` | 角色 C（运行时/IO） |
| `sim/clock.py`、`task2_scheduler/`、`task3_runtime/prefetch_runtime.py`、`task4_gpu_virt/mps_scheduler.py` | 角色 B（内存/系统） |
| 接口签名（§5 冻结契约） | 角色 A（架构师）签核，并同步更新技术方案 |

审查记录建议单独维护 `docs/互审记录.md`：一条记录包含 PR 编号、改动模块、审查人、逐条勾选结果、
发现的问题与结论（通过 / 打回）；若暂未建该文件，则在这些条目中直接写进 PR 描述。

## 4. 提交信息约定

```
<模块>: <一句话说明>

- 变更点 1
- 变更点 2

Refs: E4 / §11-3
```

模块取值：`sim`、`kv`、`sched`、`io`、`vmm`、`eval`、`docs`、`ui`。
