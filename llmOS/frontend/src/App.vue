<template>
  <div class="layout">
    <!-- ============ SIDEBAR ============ -->
    <aside>
      <div class="brand">
        <div class="logo">
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
            <rect x="2" y="2" width="12" height="12" rx="2" fill="#34e0c0" opacity="0.9" />
            <rect x="18" y="2" width="12" height="12" rx="2" fill="#9a8cff" opacity="0.55" />
            <rect x="2" y="18" width="12" height="12" rx="2" fill="#f2a94b" opacity="0.55" />
            <rect x="18" y="18" width="12" height="12" rx="2" fill="#34e0c0" opacity="0.35" />
          </svg>
          <div>
            <h1>llmOS</h1>
            <p>LLM INFERENCE · OS CONSOLE</p>
          </div>
        </div>
        <div class="brand-sub">推理操作系统 · A/B 评测台</div>
      </div>

      <nav :class="{ locked: running }">
        <div v-for="g in groups" :key="g.key" class="nav-group">
          <div class="nav-title"><i :style="{ background: g.color }"></i>{{ g.title }}</div>
          <button
            v-for="item in g.items"
            :key="item.id"
            :class="{ active: selected === item.id }"
            @click="selectExp(item.id)"
          >
            <span class="eid mono">{{ item.id }}</span>
            <span class="etitle">{{ item.title }}</span>
            <i v-if="ranIds.has(item.id)" class="ran-dot" title="已有运行记录"></i>
          </button>
        </div>
      </nav>

      <div class="aside-foot">
        <span class="dot" :class="{ on: health }"></span>
        后端 {{ health ? "ONLINE :8000" : "OFFLINE" }}
        <span class="db">· {{ health ? dbName : "—" }}</span>
      </div>
    </aside>

    <!-- ============ MAIN ============ -->
    <main>
      <header class="topbar">
        <div class="tb-left">
          <div class="crumb mono">EXPERIMENT / <b>{{ selected }}</b></div>
          <h2>{{ currentTitle }}</h2>
          <div v-if="result" class="digest mono" :title="'config digest: ' + result.config_digest">
            digest:{{ (result.config_digest || "—").slice(0, 12) }} · seed:{{ result.seed }}
          </div>
        </div>
        <div class="tb-right">
          <div class="seed-box">
            <label>seed</label>
            <input v-model.number="seed" type="number" min="1" />
            <div class="presets">
              <button v-for="s in [42, 1, 2, 123]" :key="s" :class="{ on: seed === s }" @click="seed = s">{{ s }}</button>
            </div>
          </div>
          <button class="run-btn ghost demo-btn" :disabled="running" @click="runDemo">▶ 演示四组</button>
          <button class="run-btn" :disabled="running" @click="runSelected">
            <span v-if="running" class="spin"></span>
            {{ running ? "仿真运行中" : "运行 A/B" }}
          </button>
        </div>
      </header>

      <div v-if="running" class="run-progress">
        <i></i>
        <span class="mono">{{ runLabel }}</span>
      </div>

      <p v-if="error" class="error-banner">⚠ {{ error }}</p>

      <div v-if="result" class="run-meta mono">
        <span>run #{{ activeRunId ?? "—" }}</span>
        <span>seed {{ result.seed }}</span>
        <span>{{ runAgo }}</span>
        <span v-if="result.seed !== seed" class="warn-text">seed 已改为 {{ seed }}，当前结果来自 seed {{ result.seed }}</span>
        <button class="mini" @click="runSelected">重跑</button>
      </div>

      <!-- ============ EMPTY STATE ============ -->
      <section v-if="!result" class="boot">
        <div class="boot-card">
          <div class="boot-title mono"><span class="prompt">$</span> llmOS discrete-event simulator</div>
          <p>同一 seed 冻结随机负载，左侧选择 E1–E13 任一实验，对<b>基线方案</b>与<b>操作系统级优化方案</b>做确定性 A/B 对照。</p>
          <ul class="boot-grid">
            <li><i style="background:#34e0c0"></i>KV 分页 / COW / LRU 换出</li>
            <li><i style="background:#9a8cff"></i>持续批处理 / MLFQ / FAIR</li>
            <li><i style="background:#4aa8ff"></i>分层预取 / 双缓冲 / 配额</li>
            <li><i style="background:#f2a94b"></i>VMM 超卖 / MPS / balloon</li>
          </ul>
          <button class="run-btn ghost" @click="runSelected">▶ 立即运行 {{ selected }}（seed {{ seed }}）</button>
          <p class="boot-hint mono">预计 &lt; 1 秒 · 结果自动入库，可回放 · 快捷键 R 运行 / 1–4 切任务组</p>
        </div>
        <GpuBackingPanel />
      </section>

      <template v-else>
        <!-- ============ VERDICT HERO ============ -->
        <section class="verdict-hero" :class="verdictFail ? 'warn' : 'pass'">
          <div class="vh-icon">{{ verdictFail ? "!" : "✓" }}</div>
          <div class="vh-body">
            <div class="vh-kicker mono">{{ verdictFail ? "CHECK" : "HYPOTHESIS HELD" }}</div>
            <div class="vh-text">{{ result.verdict }}</div>
          </div>
          <div class="vh-side">
            <div v-for="h in spotlights" :key="h.label" class="vh-metric">
              <span>{{ h.label }}</span>
              <b class="mono">{{ h.opt }}<em>{{ h.unit }}</em></b>
            </div>
          </div>
        </section>

        <!-- ============ KPI CARDS ============ -->
        <section class="kpi-head">
          <h3><span class="hash">#</span> 核心指标对照</h3>
          <div class="kpi-tools">
            <button class="mini" @click="showAllKpis = !showAllKpis">
              {{ showAllKpis ? "收起" : `展开全部 ${kpiTotal} 项` }}
            </button>
            <span class="mono dim">{{ metricCards.length }} / {{ kpiTotal }}</span>
          </div>
          <div class="kpi-legend mono">
            <span><i class="sw amber"></i>baseline</span>
            <span><i class="sw teal"></i>optimized</span>
            <span>Δ 为相对基线变化</span>
          </div>
        </section>
        <section class="kpis">
          <article v-for="c in metricCards" :key="c.key" class="kpi">
            <div class="kpi-label">{{ c.label }}</div>
            <div class="kpi-vals">
              <span class="mono base">{{ c.baseText }}<em>{{ c.unit }}</em></span>
              <span class="arrow">{{ c.arrow }}</span>
              <span class="mono opt">{{ c.optText }}<em>{{ c.unit }}</em></span>
            </div>
            <div class="kpi-delta" :class="c.tone">
              {{ c.deltaText }}
            </div>
          </article>
        </section>

        <!-- ============ SPECIAL: E10 share bars ============ -->
        <section v-if="result.id === 'E10'" class="panel">
          <h3><span class="hash">#</span> 份额对照 · MPS 租户累计服务时间</h3>
          <BarChart :labels="['α 租户', 'β 租户']" format="f2"
            :groups="[
              { name: '1:1 份额', color: '#8294ab', values: [result.baseline.alpha, result.baseline.beta] },
              { name: '3:1 份额', color: '#34e0c0', values: [result.optimized.alpha, result.optimized.beta] },
            ]" />
        </section>

        <!-- ============ CHARTS ============ -->
        <section v-for="ch in chartViews" :key="ch.key" class="panel" :class="{ wide: ch.wide }">
          <h3><span class="hash">#</span> {{ ch.title }}<small>{{ ch.sub }}</small></h3>
          <LineChart v-if="ch.type === 'line'" :series="ch.series" :format="ch.fmt" :xformat="ch.xfmt" :x-custom="ch.xCustom" />
          <BarChart v-else-if="ch.type === 'bar'" :labels="ch.labels" :groups="ch.groups" :format="ch.fmt" />
        </section>

        <section v-if="!chartViews.length" class="panel">
          <h3><span class="hash">#</span> 时间序列</h3>
          <p class="empty-note">该实验为结构性微基准，没有时间序列数据；结论请见上方指标对照与原始 JSON。</p>
        </section>

        <!-- ============ E7 ABLATION HEATMAP ============ -->
        <section v-if="ablation.rows.length" class="panel wide">
          <h3><span class="hash">#</span> 预取参数消融 · lookahead × cache_layers 下的 IO 停顿
            <small>越青越优，越红越差；数字为 io_stall_ratio</small>
          </h3>
          <div class="heat-wrap">
            <div class="heat" :style="{ gridTemplateColumns: `76px repeat(${ablation.cacheVals.length}, minmax(96px, 1fr))` }">
              <span class="heat-corner mono">la ＼ cl</span>
              <span v-for="cl in ablation.cacheVals" :key="cl" class="heat-col mono">{{ cl }}</span>
              <template v-for="la in ablation.laVals" :key="la">
                <span class="heat-rowhead mono">{{ la }}</span>
                <span
                  v-for="cell in ablation.grid[la]"
                  :key="cell.cl"
                  class="heat-cell mono"
                  :style="{ background: cell.color }"
                >{{ cell.v.toFixed(3) }}</span>
              </template>
            </div>
            <p v-if="ablation.note" class="heat-note">{{ ablation.note }}</p>
          </div>
        </section>

        <!-- ============ GANTT ============ -->
        <section v-if="hasGantt" class="panel wide">
          <h3>
            <span class="hash">#</span> 调度甘特
            <small>离散时钟上的 Prefill / Decode 拍，颜色区分租户</small>
          </h3>
          <div class="gantt-pair">
            <div>
              <h4 class="mono">基线侧</h4>
              <GanttChart :rows="ganttRowsFor('baseline')" />
            </div>
            <div>
              <h4 class="mono">优化侧</h4>
              <GanttChart :rows="ganttRowsFor('optimized')" />
            </div>
          </div>
        </section>

        <!-- ============ RAW CONSOLE ============ -->
        <section class="panel wide raw-panel">
          <details>
            <summary class="mono"><span class="prompt">$</span> 展开原始指标 JSON（baseline / optimized / extra）</summary>
            <div class="json-tools mono">
              <label><input v-model="showDiff" type="checkbox" /> 仅看差异</label>
              <button class="mini" @click="downloadJson">下载结果 JSON</button>
              <button class="mini" @click="downloadCharts">导出图表 PNG</button>
            </div>
            <div class="raw-grid">
              <pre class="raw amber-pre">{{ pretty(showDiff ? diffOf(result.baseline, result.optimized) : result.baseline) }}</pre>
              <pre class="raw teal-pre">{{ pretty(showDiff ? diffOf(result.optimized, result.baseline) : result.optimized) }}</pre>
            </div>
            <pre v-if="pretty(result.extra) !== '{}'" class="raw extra-pre">{{ pretty(result.extra) }}</pre>
          </details>
        </section>
      </template>

      <!-- ============ HISTORY ============ -->
      <section class="history panel">
        <h3><span class="hash">#</span> 历史运行
          <small>结果持久化在本地数据库，点击行可回放</small>
        </h3>
        <div class="hist-tools">
          <select v-model="histFilter" class="mono">
            <option value="">全部实验</option>
            <option v-for="id in histIds" :key="id" :value="id">{{ id }}</option>
          </select>
          <label class="mono"><input v-model="starOnly" type="checkbox" /> 只看收藏</label>
          <span class="mono dim">{{ filteredRuns.length }} 条</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th class="starcol"></th><th>RUN</th><th>实验</th><th>SEED</th><th>时间</th><th>结论 / verdict</th></tr>
            </thead>
            <tbody>
              <tr
                v-for="r in filteredRuns"
                :key="r.run_id"
                :class="{ 'active-run': activeRunId === r.run_id }"
                @click="openRun(r.run_id)"
              >
                <td class="starcol">
                  <button class="star-btn" :class="{ on: starred.has(r.run_id) }" @click.stop="toggleStar(r.run_id)">
                    {{ starred.has(r.run_id) ? "★" : "☆" }}
                  </button>
                </td>
                <td class="mono">#{{ r.run_id }}</td>
                <td><span class="eid-chip mono">{{ r.exp_id }}</span> {{ r.title }}</td>
                <td class="mono">{{ r.seed }}</td>
                <td class="mono dim">{{ fmtTime(r.created_at) }}</td>
                <td><span class="vchip" :class="isFailVerdict(r.verdict) ? 'warn' : 'pass'">{{ r.verdict }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import LineChart from "./components/LineChart.vue";
import BarChart from "./components/BarChart.vue";
import GanttChart from "./components/GanttChart.vue";
import GpuBackingPanel from "./components/GpuBackingPanel.vue";

/* ---------------- experiment taxonomy ---------------- */
const GROUP_DEF = [
  { key: "mem", title: "任务一 · KV 显存分页", color: "#34e0c0", ids: ["E1", "E2", "E3", "E11"] },
  { key: "sched", title: "任务二 · 批处理调度", color: "#9a8cff", ids: ["E4", "E5", "E6"] },
  { key: "rt", title: "任务三 · 分层预取运行时", color: "#4aa8ff", ids: ["E7", "E12"] },
  { key: "virt", title: "任务四 · GPU 虚拟化", color: "#f2a94b", ids: ["E8", "E9", "E10", "E13"] },
];

/* ---------------- metric registry ---------------- */
const M = {
  throughput: ["吞吐量", "req/s", 1, "f2"],
  completed: ["完成请求", "", 1, "int"],
  oom: ["OOM / 拒绝", "次", -1, "int"],
  oom_ratio: ["OOM 率", "", -1, "pct"],
  ttft_p50: ["TTFT P50", "s", -1, "f3"],
  ttft_p99: ["TTFT P99", "s", -1, "f3"],
  latency_p50: ["端到端延迟 P50", "s", -1, "f3"],
  latency_p99: ["端到端延迟 P99", "s", -1, "f3"],
  kv_utilization_peak: ["KV 峰值利用率", "", 1, "pct"],
  kv_fragmentation_peak: ["KV 峰值碎片率", "", -1, "pct"],
  kv_evictions: ["KV 换出次数", "", 0, "int"],
  peak_concurrency: ["峰值并发", "", 0, "int"],
  peak_live_seqs: ["峰值驻留序列", "", 0, "int"],
  hunger_ratio: ["饥饿请求率", "", -1, "pct"],
  gpu_busy_ratio: ["GPU 忙占比", "", 0, "pct"],
  tenant_p99_var: ["租户 P99 方差", "", -1, "var"],
  fairness: ["Jain 公平指数", "", 1, "f3"],
  io_stall_ratio: ["IO 停顿占比", "", -1, "pct"],
  effective_compute_ratio: ["有效算力占比", "", 1, "pct"],
  cache_hit_rate: ["缓存命中率", "", 1, "pct"],
  cache_hits: ["缓存命中", "次", 1, "int"],
  ctx_switch_count: ["上下文切换", "次", -1, "int"],
  ctx_switch_overhead_ratio: ["切换开销占比", "", -1, "pct"],
  physical_blocks: ["占用物理块", "块", -1, "int"],
  branches: ["采样分支", "", 0, "int"],
  oversub: ["超卖倍数", "×", 0, "f2"],
  vmm_time: ["虚拟化耗时", "s", -1, "f3"],
  isolation_intercept_rate: ["越界拦截率", "", 1, "pct"],
  page_faults: ["缺页次数", "", 0, "int"],
  swap_outs: ["换出页数", "", 0, "int"],
  blocked_cross_tenant: ["拦截越界", "", 0, "int"],
  balloon_reclaimed: ["balloon 回收", "页", 0, "int"],
  max_model_gb: ["可载模型规模", "GB", 1, "f1"],
  hbm_gb: ["HBM 容量", "GB", 0, "f1"],
  alpha: ["α 累计服务", "s", 0, "f2"],
  beta: ["β 累计服务", "s", 0, "f2"],
  stall: ["IO 停顿", "s", -1, "f3"],
  compute: ["有效计算", "s", 1, "f3"],
};
const METRIC_ORDER = [
  "throughput", "completed", "oom", "oom_ratio",
  "ttft_p50", "ttft_p99", "latency_p50", "latency_p99",
  "kv_utilization_peak", "kv_fragmentation_peak", "kv_evictions",
  "peak_concurrency", "peak_live_seqs", "hunger_ratio", "gpu_busy_ratio",
  "tenant_p99_var", "fairness",
  "io_stall_ratio", "effective_compute_ratio", "cache_hit_rate",
  "ctx_switch_count", "ctx_switch_overhead_ratio",
  "physical_blocks", "branches",
  "oversub", "vmm_time", "isolation_intercept_rate",
  "page_faults", "swap_outs", "blocked_cross_tenant", "balloon_reclaimed",
  "max_model_gb", "hbm_gb", "alpha", "beta", "stall", "compute",
];

function fmtVal(v, kind) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  v = Number(v);
  if (kind === "pct") return (v * 100).toFixed(1) + "%";
  if (kind === "int") return String(Math.round(v));
  if (kind === "f1") return v.toFixed(1);
  if (kind === "f2") return v.toFixed(2);
  if (kind === "f3") return v.toFixed(3);
  if (kind === "var") return Math.abs(v) < 0.001 ? v.toExponential(1) : v.toFixed(4);
  return String(v);
}

/* ---------------- state ---------------- */
const catalog = ref([]);
const selected = ref("E1");
const seed = ref(42);
const running = ref(false);
const health = ref(false);
const dbName = ref("sqlite");
const error = ref("");
const result = ref(null);
const runs = ref([]);
const showAllKpis = ref(false);
const showDiff = ref(false);
const histFilter = ref("");
const starOnly = ref(false);
const starred = ref(new Set());
const activeRunId = ref(null);
const runAt = ref(0);
const nowTick = ref(Date.now());
const demoState = ref(null);
let token = 0;
let ticker = null;
const FAV_KEY = "llmos.starred";

const groups = computed(() =>
  GROUP_DEF.map((g) => ({ ...g, items: catalog.value.filter((x) => g.ids.includes(x.id)) })).filter(
    (g) => g.items.length
  )
);
const currentTitle = computed(() => {
  const hit = catalog.value.find((x) => x.id === selected.value);
  return hit ? hit.title : "选择实验";
});

/* ---------------- verdict ---------------- */
function isFailVerdict(v) {
  return /未|需检查|仍需/.test(String(v || ""));
}
const verdictFail = computed(() => isFailVerdict(result.value?.verdict));

/* ---------------- spotlight numbers on hero ---------------- */
const spotlights = computed(() => {
  if (!result.value) return [];
  const b = result.value.baseline, o = result.value.optimized, x = result.value.extra || {};
  const out = [];
  if (o.max_model_gb !== undefined)
    out.push({ label: "单卡可载模型", opt: fmtVal(o.max_model_gb, "f1"), unit: " GB" });
  if (o.physical_blocks !== undefined)
    out.push({ label: "占用物理块", opt: fmtVal(o.physical_blocks, "int"), unit: " 块" });
  if (o.isolation_intercept_rate !== undefined)
    out.push({ label: "越界拦截率", opt: fmtVal(o.isolation_intercept_rate, "pct"), unit: "" });
  if (o.cache_hit_rate !== undefined)
    out.push({ label: "权重缓存命中", opt: fmtVal(o.cache_hit_rate, "pct"), unit: "" });
  if (x.optimized_tenants !== undefined)
    out.push({ label: "同卡租户数", opt: x.optimized_tenants, unit: " 个" });
  return out.slice(0, 3);
});

/* ---------------- KPI cards ---------------- */
const metricCards = computed(() => {
  if (!result.value) return [];
  const b = result.value.baseline, o = result.value.optimized;
  const keys = METRIC_ORDER.filter((k) => k in b && k in o && k in M);
  const shown = showAllKpis.value ? keys : keys.slice(0, 12);
  return shown.map((k) => {
    const [label, unit, dir, kind] = M[k];
    const bv = Number(b[k]), ov = Number(o[k]);
    const d = ov - bv;
    const eps = 1e-12;
    let tone = "flat", arrow = "→";
    if (dir > 0 && d > eps) { tone = "good"; arrow = "↑"; }
    else if (dir < 0 && d < -eps) { tone = "good"; arrow = "↓"; }
    else if (dir > 0 && d < -eps) { tone = "bad"; arrow = "↓"; }
    else if (dir < 0 && d > eps) { tone = "bad"; arrow = "↑"; }
    let deltaText = "持平";
    if (tone !== "flat") {
      const rel = Math.abs(bv) > 1e-9 ? (Math.abs(d) / Math.abs(bv)) * 100 : Math.abs(d) * 100;
      deltaText = (tone === "good" ? "改善 " : "劣化 ") + rel.toFixed(rel >= 100 ? 0 : 1) + "%";
    }
    return { key: k, label, unit, baseText: fmtVal(bv, kind), optText: fmtVal(ov, kind), tone, arrow, deltaText };
  });
});
const kpiTotal = computed(() => {
  if (!result.value) return 0;
  const b = result.value.baseline, o = result.value.optimized;
  return METRIC_ORDER.filter((k) => k in b && k in o && k in M).length;
});

/* ---------------- charts ---------------- */
const COL = { base: "#f2a94b", opt: "#34e0c0", double: "#6b7a90" };

const chartViews = computed(() => {
  if (!result.value) return [];
  const charts = result.value.charts || {};
  const views = [];

  if (charts.kv_util && (charts.kv_util.baseline?.length || charts.kv_util.optimized?.length)) {
    views.push({
      key: "kv_util", type: "line", title: "KV 池利用率轨迹", sub: "离散时钟推进下的块池占用",
      fmt: "pct", xfmt: "f3",
      series: [
        { name: "基线", color: COL.base, points: charts.kv_util.baseline || [] },
        { name: "优化", color: COL.opt, points: charts.kv_util.optimized || [], area: true },
      ],
    });
  }
  if (charts.kv_frag && (charts.kv_frag.baseline?.length || charts.kv_frag.optimized?.length)) {
    views.push({
      key: "kv_frag", type: "line", title: "KV 碎片率轨迹", sub: "末块空洞占已分配空间比例",
      fmt: "pct", xfmt: "f3",
      series: [
        { name: "基线", color: COL.base, points: charts.kv_frag.baseline || [] },
        { name: "优化", color: COL.opt, points: charts.kv_frag.optimized || [], area: true },
      ],
    });
  }
  if (charts.stall_timeline) {
    const s = charts.stall_timeline;
    views.push({
      key: "stall", type: "line", title: "分层预取 · 每拍 DMA 停顿", sub: "串行 / 双缓冲 / 预取+缓存 三方案",
      fmt: "f3", xfmt: "int", wide: true,
      series: [
        { name: "串行加载", color: COL.base, points: s.baseline || [] },
        { name: "双缓冲", color: COL.double, points: s.double || [], dash: "5 4" },
        { name: "预取 + 缓存", color: COL.opt, points: s.optimized || [], area: true },
      ],
    });
  }
  if (charts.tenant_p99) {
    const c = charts.tenant_p99;
    views.push({
      key: "tp99", type: "bar", title: "跨租户 P99 延迟", sub: "突发负载下 FCFS vs FAIR（1:1 份额）",
      fmt: "f3", wide: true, labels: c.tenants,
      groups: [
        { name: "FCFS", color: COL.base, values: c.baseline },
        { name: "FAIR", color: COL.opt, values: c.optimized },
      ],
    });
  }
  if (charts.oversub_latency && charts.oversub_latency.length) {
    const pts = charts.oversub_latency;
    const xs = pts.map((p) => Number(p[0]));
    views.push({
      key: "pareto", type: "line", title: "超卖比 – 虚拟化延迟 Pareto", sub: "物理帧越紧张，缺页换入与脏页写回越贵",
      fmt: "f3", xfmt: "f1", wide: true,
      xCustom: [Math.min(...xs).toFixed(1), ((Math.min(...xs) + Math.max(...xs)) / 2).toFixed(1), Math.max(...xs).toFixed(1) + "×"],
      series: [{ name: "vmm_time", color: COL.opt, points: pts, area: true }],
    });
  }
  return views;
});

/* ---------------- E7 ablation heatmap ---------------- */
const ablation = computed(() => {
  const rows = result.value?.extra?.ablation;
  if (!rows || !rows.length) return { rows: [], grid: {}, laVals: [], cacheVals: [], note: "" };
  const laVals = [...new Set(rows.map((r) => r.lookahead))].sort((a, b) => a - b);
  const cacheVals = [...new Set(rows.map((r) => r.cache_layers))].sort((a, b) => a - b);
  const vals = rows.map((r) => r.io_stall_ratio);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const colorFor = (v) => {
    const t = hi - lo < 1e-9 ? 0 : (v - lo) / (hi - lo);
    // teal (#34e0c0) -> amber (#f2a94b) -> red (#ff5f72)
    const c1 = [52, 224, 192], c2 = [242, 169, 75], c3 = [255, 95, 114];
    const mix = (a, b, k) => a.map((x, i) => Math.round(x + (b[i] - x) * k));
    const rgb = t < 0.5 ? mix(c1, c2, t * 2) : mix(c2, c3, (t - 0.5) * 2);
    return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.28)`;
  };
  const grid = {};
  for (const la of laVals) {
    grid[la] = rows.filter((r) => r.lookahead === la).map((r) => ({
      cl: r.cache_layers, v: r.io_stall_ratio, color: colorFor(r.io_stall_ratio),
    }));
  }
  return {
    rows, grid, laVals, cacheVals,
    note: result.value?.extra?.interpretation || "",
  };
});

/* ---------------- gantt ---------------- */
const hasGantt = computed(() => {
  const g = result.value?.gantt;
  return Boolean(g && ((g.baseline && g.baseline.length) || (g.optimized && g.optimized.length)));
});
function ganttRowsFor(side) {
  const g = result.value?.gantt?.[side];
  if (!g || !g.length) return [];
  return g.slice(-26).map((r) => ({
    t: Number(r.t).toFixed(4),
    kind: r.kind,
    tenants: r.tenants || [],
    rids: r.rids || [],
    stall: Number(r.stall || 0),
  }));
}

/* ---------------- run state computed ---------------- */
const ranIds = computed(() => new Set(runs.value.map((r) => r.exp_id)));
const histIds = computed(() => [...new Set(runs.value.map((r) => r.exp_id))].sort());
const filteredRuns = computed(() => {
  let list = runs.value;
  if (histFilter.value) list = list.filter((r) => r.exp_id === histFilter.value);
  if (starOnly.value) list = list.filter((r) => starred.value.has(r.run_id));
  return [...list].sort((a, b) => Number(b.run_id) - Number(a.run_id));
});
const runAgo = computed(() => {
  if (!runAt.value) return "—";
  const s = Math.max(0, Math.round((nowTick.value - runAt.value) / 1000));
  if (s < 10) return "刚刚运行";
  if (s < 60) return `${s} 秒前`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} 分钟前` : `${Math.round(m / 60)} 小时前`;
});
const runLabel = computed(() =>
  demoState.value ? `演示 ${demoState.value.i}/${demoState.value.n} · ${selected.value}` : `运行 ${selected.value}`
);

/* ---------------- helpers ---------------- */
function pretty(obj) {
  const skip = new Set(["timeline"]);
  const slim = {};
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (skip.has(k)) return;
    if (typeof v !== "object" || v === null) slim[k] = v;
    else if (!Array.isArray(v)) slim[k] = v;
  });
  return JSON.stringify(slim, null, 2);
}
function fmtTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19);
}

async function api(path, opts) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  return res.json();
}

async function refresh() {
  try {
    const h = await api("/api/health");
    health.value = !!h.ok;
    dbName.value = h.db || "db";
    catalog.value = (await api("/api/experiments")).items;
    runs.value = (await api("/api/runs")).items;
    error.value = "";
  } catch {
    health.value = false;
    error.value = "无法连接后端，请确认 FastAPI 已在 :8000 启动";
  }
}

function selectExp(id) {
  if (running.value || id === selected.value) return;
  selected.value = id;
  result.value = null;
  activeRunId.value = null;
  error.value = "";
  syncUrl();
}

async function doRun(expId, s) {
  const data = await api("/api/experiments/run", {
    method: "POST",
    body: JSON.stringify({ exp_id: expId, seed: s }),
  });
  result.value = data.result;
  activeRunId.value = data.run_id ?? null;
  runAt.value = Date.now();
  nowTick.value = Date.now();
  syncUrl();
  await refresh();
}

async function runSelected() {
  if (running.value) return;
  const my = ++token;
  running.value = true;
  error.value = "";
  try {
    await doRun(selected.value, seed.value);
  } catch (e) {
    if (my === token) error.value = e.message || String(e);
  } finally {
    if (my === token) running.value = false;
  }
}

async function runDemo() {
  if (running.value) return;
  const ids = ["E1", "E4", "E7", "E9"];
  const my = ++token;
  running.value = true;
  error.value = "";
  try {
    for (let i = 0; i < ids.length; i++) {
      if (my !== token) return;
      demoState.value = { i: i + 1, n: ids.length };
      selected.value = ids[i];
      result.value = null;
      activeRunId.value = null;
      await doRun(ids[i], seed.value);
    }
  } catch (e) {
    if (my === token) error.value = e.message || String(e);
  } finally {
    if (my === token) {
      running.value = false;
      demoState.value = null;
    }
  }
}

async function openRun(id) {
  try {
    const data = await api(`/api/runs/${id}`);
    selected.value = data.exp_id;
    result.value = data.result;
    activeRunId.value = data.run_id ?? id;
    const meta = runs.value.find((r) => String(r.run_id) === String(id));
    runAt.value = meta?.created_at ? Date.parse(meta.created_at) : Date.now();
    nowTick.value = Date.now();
    syncUrl();
    error.value = "";
  } catch (e) {
    error.value = e.message || String(e);
  }
}

function syncUrl() {
  const p = new URLSearchParams();
  p.set("exp", selected.value);
  p.set("seed", String(seed.value));
  if (activeRunId.value != null) p.set("run", String(activeRunId.value));
  history.replaceState(null, "", "?" + p.toString());
}

function toggleStar(id) {
  const s = new Set(starred.value);
  if (s.has(id)) s.delete(id);
  else s.add(id);
  starred.value = s;
  localStorage.setItem(FAV_KEY, JSON.stringify([...s]));
}

function diffOf(a, b) {
  const out = {};
  for (const [k, v] of Object.entries(a || {})) {
    if (JSON.stringify(v) !== JSON.stringify(b?.[k])) out[k] = v;
  }
  return out;
}

function downloadJson() {
  if (!result.value) return;
  const blob = new Blob([JSON.stringify(result.value, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${result.value.id}_seed${result.value.seed}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function svgToPng(svg, name) {
  const clone = svg.cloneNode(true);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
  style.textContent =
    ".axis{fill:#8294ab;font-size:9px;font-family:ui-monospace,monospace}" +
    ".grid{stroke:#1d2a3c;stroke-width:1}.grid.base{stroke:#31445f}" +
    ".cursor{stroke:#4b637f;stroke-width:1;stroke-dasharray:3 3}" +
    ".hdot{stroke:#0b1119;stroke-width:1}";
  clone.appendChild(style);
  const xml = new XMLSerializer().serializeToString(clone);
  const url = URL.createObjectURL(new Blob([xml], { type: "image/svg+xml;charset=utf-8" }));
  const img = new Image();
  await new Promise((res, rej) => {
    img.onload = res;
    img.onerror = rej;
    img.src = url;
  });
  const vb = (svg.getAttribute("viewBox") || "0 0 660 232").split(/\s+/).map(Number);
  const canvas = document.createElement("canvas");
  canvas.width = vb[2] * 2;
  canvas.height = vb[3] * 2;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#0b1119";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  URL.revokeObjectURL(url);
  const a = document.createElement("a");
  a.href = canvas.toDataURL("image/png");
  a.download = name;
  a.click();
}

async function downloadCharts() {
  const svgs = [...document.querySelectorAll("main .panel svg")];
  if (!svgs.length) return;
  for (let i = 0; i < svgs.length; i++) {
    await svgToPng(svgs[i], `${result.value?.id || "chart"}_${i + 1}.png`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

function onKey(e) {
  const tag = (e.target?.tagName || "").toUpperCase();
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "r" || e.key === "R") {
    e.preventDefault();
    runSelected();
    return;
  }
  const n = Number(e.key);
  if (n >= 1 && n <= 4) {
    const g = groups.value[n - 1];
    if (g?.items?.length) selectExp(g.items[0].id);
  }
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  const exp = p.get("exp");
  if (exp && /^E\d+$/.test(exp)) selected.value = exp;
  const sd = Number(p.get("seed"));
  if (Number.isFinite(sd) && sd > 0) seed.value = sd;
  const run = p.get("run");
  if (run) openRun(run);
}

watch(seed, syncUrl);

onMounted(async () => {
  try {
    starred.value = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || "[]"));
  } catch {
    starred.value = new Set();
  }
  await refresh();
  readUrl();
  window.addEventListener("keydown", onKey);
  ticker = setInterval(() => (nowTick.value = Date.now()), 20000);
});

onUnmounted(() => {
  window.removeEventListener("keydown", onKey);
  if (ticker) clearInterval(ticker);
});
</script>
