# GPU 对照技术方案

**当前状态：延期。不下载权重、不装 transformers/vLLM、不跑 Colab。** 仿真评测以 `results/E*.json` 为准。

**地位：** 仿真（llmOS）为主交付；本文是零硬件清单里「真实 GPU + 推理框架」的**预留方案**，失败不阻塞答辩。  
**原则：** 斜杠表示三选一。将来若做：GPU 选 Colab T4；框架用本地权重（非在线 API）：transformers 基线 + vLLM 优化。

---

## 1 目标与非目标

**要证明的方向**

| 仿真 | GPU 上希望看到的方向 |
| --- | --- |
| E1 连续预占 vs KV 分页 | 连续 KV 并发上不去或更易 OOM；分页引擎同显存可挂更多序列 |
| E4 静态批 vs 持续批 | 持续批吞吐更高，短请求 TTFT 更好 |

**明确不做**

- 不在 GPU 上复现任务三预取、任务四超卖/MIG/balloon（T4 无 MIG，vLLM 也不是这套接口）。
- 不追求与 `results/E*.json` 数字一致。
- 不上 7B 全精度、不租 AutoDL（Colab 不够再用 Kaggle）。

---

## 2 选型理由

| 项 | 选择 | 原因 |
| --- | --- | --- |
| GPU | Google Colab T4 16GB | 免费；一张 `nvidia-smi` 即可交差。Kaggle 为备选。 |
| 基线框架 | HuggingFace transformers | 默认连续 KV + 整批 `generate`，对应仿真 contiguous / 静态批。安装简单。 |
| 优化框架 | vLLM | 现成 PagedAttention + 持续批，对应任务一、二的参考实现。 |
| 不选 llama.cpp | — | 主线是量化与 CPU 卸载，对不上页表和 iteration 组批。 |
| 模型 | `Qwen/Qwen2-0.5B-Instruct`（备选 `facebook/opt-125m`） | T4 上 fp16 可同时跑两套脚本，避免显存噪声淹没结论。 |

同一 Colab 会话、同一模型、同一 prompt 列表、同一 `max_new_tokens`，只换引擎。

---

## 3 对照架构

```
合成 prompt 列表（与仿真 W1 同分布：prompt 32–128 token 量级，生成 32–64）
        |
        +--> [A] transformers.generate  连续 KV，固定 batch
        |         指标：峰值显存、OOM 次数、整批墙钟吞吐、首 token 粗测
        |
        +--> [B] vLLM LLM.generate      PagedAttention + 持续批
                  指标：峰值显存、OOM 次数、req/s、TTFT（引擎统计）
        |
        v
  只比较方向：B 的并发/吞吐不低于 A，OOM 不高于 A
  记录：GPU 名、驱动、CUDA、transformers/vLLM/torch 版本、模型、精度
```

仿真内核（`heapq` 离散事件）与 GPU 脚本解耦：GPU 脚本不 import `llmos`，结果单独写入 `gpu_results/`，评测报告附录引用。

---

## 4 实验设计

### G1 对应 E1（显存与并发）

- 固定 T4、fp16、`max_new_tokens=64`。
- 并发序列数 `N = 4, 8, 16, 32`（装不下就停）。
- **A：** transformers，每条序列独立 `past_key_values`（或一次 pad 成稠密 batch，预留最大长度），模拟连续预占。
- **B：** vLLM，`gpu_memory_utilization=0.9`，默认分页。
- **主指标：** 成功完成的最大 N、是否 OOM、峰值显存。
- **通过线：** B 的最大 N ≥ A，或 A 先 OOM。

### G2 对应 E4（调度形态）

- 固定 N（G1 中两边都能跑完的最大 N，建议 8）。
- 到达：一次性提交 vs 交错提交（模拟持续到达）。
- **A：** 凑满 N 再 `generate`（静态批）。
- **B：** vLLM 持续批（请求一到就进引擎）。
- **主指标：** 墙钟吞吐（完成请求数/秒）、粗 TTFT（首包时间）。
- **通过线：** B 吞吐 ≥ A。

### 不对照的任务

| 任务 | 原因 | 仍用仿真 |
| --- | --- | --- |
| 预取 / stall | 真机权重一般已在 GPU | E7 |
| 配额 / 多租户 P99 | 单进程单模型 | E6/E8 |
| 超卖 / balloon / 越界 | 无 GPA 层 | E9 |
| MPS 3:1 | 消费级 T4 无 MIG | E10 |

---

## 5 环境与步骤

1. Colab 选 T4，执行 `!nvidia-smi`。
2. 安装（版本随 Colab CUDA 微调，记入报告）：

```text
pip install -U transformers accelerate torch
pip install vllm
```

3. 跑 G1、G2，JSON 落地，字段至少包括：`engine`, `model`, `n`, `max_new_tokens`, `ok`, `oom`, `elapsed_s`, `peak_mem_gb`, `notes`。
4. 若 vLLM 与当前 CUDA 轮子不匹配：只交 G1 的 transformers 基线，报告写「优化侧环境限制，仿真 E1/E4 为准」。

---

## 6 指标与仿真的关系

| GPU 字段 | 仿真字段 | 关系 |
| --- | --- | --- |
| 峰值显存、最大 N、OOM | `kv_utilization_peak`、`peak_concurrency`、`oom_ratio` | 只比升降方向 |
| req/s、TTFT | `throughput`、`ttft_p50` | 只比升降方向 |
| — | `io_stall_ratio`、`oversubscription_ratio`、`fairness` | GPU 不做 |

禁止把 GPU 墙钟吞吐写进仿真 JSON 当同一列数字。

---

## 7 风险

| 风险 | 处理 |
| --- | --- |
| Colab 抢不到 T4 | 改 Kaggle；仍失败则附录注明，主文只保留仿真 |
| vLLM 安装失败 | 仅 transformers；结论降级为「连续 KV 基线已实测」 |
| 0.5B 太轻，E1 两边都不 OOM | 加大 `max_new_tokens` 或 N；或改 1.5B 量化，不改题目口径 |
| 数字和仿真差数量级 | 预期内：仿真是抽象 step_cost，GPU 是真实 kernel |

---

## 8 交付物

- 本方案文档（本文）
- Colab 笔记本或 `scripts/gpu_g1_g2.py`（实现阶段再补）
- `gpu_results/g1.json`、`g2.json` + `nvidia-smi` 文本
- 评测报告附录一节「云端方向对照」

主评测报告仍只引用 `backend/results/E*.json`。GPU 数字进附录，避免和黄金 trace 混表。
