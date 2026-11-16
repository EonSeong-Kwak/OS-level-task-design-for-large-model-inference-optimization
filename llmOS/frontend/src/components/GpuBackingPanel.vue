<template>
  <section class="gp-panel" :class="{ running: s.status === 'running', disabled: !s.available }">
    <!-- header -->
    <header class="gp-head">
      <div class="gp-gpu" :class="s.status">
        <span class="gp-ring r1"></span>
        <span class="gp-ring r2"></span>
        <svg viewBox="0 0 24 24" class="gp-die" width="30" height="30">
          <rect x="4" y="4" width="16" height="16" rx="2"
                fill="none" stroke="currentColor" stroke-width="1.4"/>
          <rect x="8" y="8" width="8" height="8" rx="1" fill="currentColor" opacity=".35"/>
          <g fill="currentColor">
            <rect x="2" y="8" width="2" height="3"/><rect x="2" y="13" width="2" height="3"/>
            <rect x="20" y="8" width="2" height="3"/><rect x="20" y="13" width="2" height="3"/>
            <rect x="8" y="2" width="3" height="2"/><rect x="13" y="2" width="3" height="2"/>
            <rect x="8" y="20" width="3" height="2"/><rect x="13" y="20" width="3" height="2"/>
          </g>
        </svg>
        <div class="gp-cores">
          <i v-for="n in 12" :key="n" :style="{ animationDelay: (n * 0.12) + 's' }"></i>
        </div>
      </div>
      <div class="gp-headtext">
        <div class="gp-title">
          真机 GPU 真实性背书
          <span class="gp-badge mono" :class="s.status">{{ statusText }}</span>
        </div>
        <div class="gp-sub mono">
          <template v-if="s.available">{{ s.name || "CUDA GPU" }}</template>
          <template v-else>未检测到 CUDA GPU —— 该功能在本机不可用（仿真主交付不受影响）</template>
        </div>
      </div>
    </header>

    <!-- idle -->
    <div v-if="s.status === 'idle' && !s.g1" class="gp-body">
      <p class="gp-desc">
        在真实 GPU 上加载本地 Qwen2.5-0.5B，复现技术方案 §6.3 的 <b>G1 对照</b>
        （静态 KV 预占 vs 按需分配，对应仿真 E1）与<b>代价参数校准</b>。
        离线运行、独立子进程，<em>不进入仿真链路、不与 results/E*.json 混表</em>。
      </p>
      <details class="gp-adv">
        <summary class="mono">高级设置</summary>
        <label>模型路径 / HF id
          <input v-model="model" class="gp-input mono" spellcheck="false"/>
        </label>
        <label>模式
          <select v-model="mode" class="gp-input">
            <option value="all">G1 + 校准（约 1 分钟）</option>
            <option value="g1">仅 G1 对照</option>
            <option value="calib">仅参数校准</option>
          </select>
        </label>
      </details>
      <button class="gp-run" :disabled="!s.available" @click="launch">
        <span class="gp-bolt">⚡</span> 调用本地模型运行真机对照
      </button>
    </div>

    <!-- running -->
    <div v-else-if="s.status === 'running'" class="gp-body">
      <div class="gp-stage">
        <span class="gp-stage-dot"></span>
        <span class="mono">{{ stage.label }}</span>
        <span class="gp-dots"><i></i><i></i><i></i></span>
      </div>
      <div class="gp-track">
        <div class="gp-fill" :style="{ width: stage.pct + '%' }">
          <span class="gp-shimmer"></span>
        </div>
        <div class="gp-ticks">
          <i v-for="n in 9" :key="n" :class="{ on: stage.steps >= n }"></i>
        </div>
      </div>
      <div class="gp-term">
        <div class="gp-term-bar mono"><span class="gp-tl">●</span> gpu_g1.py — live output
          <span class="gp-pid mono" v-if="s.started_at">{{ elapsed }}s</span></div>
        <transition-group tag="div" name="gpline" class="gp-lines">
          <div v-for="(line, i) in termLines" :key="i + ':' + line"
               class="gp-line mono" :class="kindOf(line)">{{ line || "&nbsp;" }}</div>
        </transition-group>
      </div>
    </div>

    <!-- done / error / idle-with-prior-result -->
    <div v-else class="gp-body">
      <!-- error banner -->
      <div v-if="s.status === 'error'" class="gp-error">
        运行失败（exit {{ s.returncode }}），见终端日志最后一行；可重试。
      </div>

      <div v-if="s.g1" class="gp-result">
        <div class="gp-verdict" :class="s.g1.verdict === 'CONSISTENT_WITH_SIM_E1' ? 'ok' : 'bad'">
          <span class="gp-check">{{ s.g1.verdict === 'CONSISTENT_WITH_SIM_E1' ? "✓" : "!" }}</span>
          <div>
            <div class="mono gp-v-kicker">{{ s.g1.verdict }}</div>
            <div class="gp-v-text">真实引擎方向{{ s.g1.verdict === 'CONSISTENT_WITH_SIM_E1' ? "与仿真 E1 一致" : "与仿真不一致，需检查" }}</div>
          </div>
        </div>

        <div class="gp-stats">
          <div class="gp-stat amber">
            <span>静态预占 · 最大并发</span>
            <b class="mono">{{ s.g1.max_concurrency.static_prealloc }}</b>
          </div>
          <div class="gp-vs">→</div>
          <div class="gp-stat teal">
            <span>按需分配 · 最大并发</span>
            <b class="mono">{{ s.g1.max_concurrency.dynamic_ondemand }}</b>
          </div>
          <div class="gp-stat gain">
            <span>承载提升</span>
            <b class="mono">×{{ s.g1.concurrency_gain_x }}</b>
          </div>
        </div>

        <svg viewBox="0 0 280 104" class="gp-spark" aria-label="G1 memory curve">
          <defs>
            <linearGradient id="gpTealFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#34e0c0" stop-opacity=".35"/>
              <stop offset="100%" stop-color="#34e0c0" stop-opacity="0"/>
            </linearGradient>
          </defs>
          <g v-for="gy in [20, 44, 68, 92]" :key="gy">
            <line x1="30" :y1="gy" x2="272" :y2="gy" stroke="#1d2a3c" stroke-width="1"/>
          </g>
          <line v-if="vramY" x1="30" :y1="vramY" x2="272" :y2="vramY"
                stroke="#8a97a8" stroke-width="1" stroke-dasharray="3 3"/>
          <text v-if="vramY" x="32" :y="vramY - 3" fill="#8a97a8" font-size="8">
            VRAM {{ s.g1.dedicated_vram_gb }}GB
          </text>
          <polygon :points="tealArea" fill="url(#gpTealFill)"/>
          <polyline :points="amberPts" fill="none" stroke="#f2a94b" stroke-width="2"/>
          <polyline :points="tealPts" fill="none" stroke="#34e0c0" stroke-width="2"/>
          <circle v-for="p in amberPtsArr" :key="'a'+p.n" :cx="p.x" :cy="p.y" r="2.6" fill="#f2a94b"/>
          <circle v-for="p in tealPtsArr" :key="'t'+p.n" :cx="p.x" :cy="p.y" r="2.6" fill="#34e0c0"/>
          <g v-for="p in oomMarks" :key="'o'+p.n+p.engine">
            <line :x1="p.x - 4" :y1="p.y - 4" :x2="p.x + 4" :y2="p.y + 4" stroke="#ff5f72" stroke-width="1.6"/>
            <line :x1="p.x - 4" :y1="p.y + 4" :x2="p.x + 4" :y2="p.y - 4" stroke="#ff5f72" stroke-width="1.6"/>
          </g>
          <text x="30" y="102" fill="#7c8aa0" font-size="8">并发 N →</text>
          <text x="2" y="18" fill="#7c8aa0" font-size="8">GB</text>
        </svg>
        <div class="gp-legend mono">
          <span><i style="background:#f2a94b"></i>StaticCache 预占 {{ s.g1.reserved_budget_per_seq }}tok</span>
          <span><i style="background:#34e0c0"></i>DynamicCache 按需</span>
          <span><i class="gp-x"></i>OOM/溢出</span>
        </div>

        <div v-if="s.calibration" class="gp-calib">
          <div class="gp-chip mono">
            prefill 拟合 <b>{{ pf.fixed }}ms + {{ pf.slope }}ms/tok</b>
            <em>R²={{ pf.r2 }}</em>
          </div>
          <div class="gp-chip mono">
            decode 拟合 <b>{{ dc.fixed }}ms + {{ dc.slope }}ms/B</b>
            <em>R²={{ dc.r2 }}</em>
          </div>
          <div class="gp-chip mono">KV <b>{{ kvTok }}</b> B/token</div>
        </div>

        <div class="gp-env mono" v-if="s.g1.environment">
          {{ s.g1.environment.gpu }} · {{ shortModel }} · {{ s.g1.environment.dtype }}
          · torch {{ s.g1.environment.torch }} · tf {{ s.g1.environment.transformers }}
        </div>
      </div>

      <div v-else-if="s.status !== 'error'" class="gp-empty mono">
        尚无背书结果 —— 点击运行开始第一次真机对照。
      </div>

      <div v-if="lastLines.length" class="gp-term collapsed">
        <div class="gp-term-bar mono"><span class="gp-tl">●</span> last run log</div>
        <div class="gp-lines">
          <div v-for="(line, i) in lastLines" :key="i" class="gp-line mono"
               :class="kindOf(line)">{{ line }}</div>
        </div>
      </div>

      <button class="gp-run ghost" @click="launch">
        <span class="gp-bolt">↻</span> 重新运行真机对照
      </button>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

const s = reactive({
  status: "idle", available: null, name: "", default_model: "",
  g1: null, calibration: null, log: [], started_at: null, returncode: null,
});
const model = ref("");
const mode = ref("all");
let timer = null;
const startedAt = ref(null);
const tickNow = ref(0);

async function fetchStatus() {
  const res = await fetch("/api/gpu-backing");
  if (!res.ok) throw new Error("gpu-backing status failed");
  const d = await res.json();
  Object.assign(s, {
    status: d.status, available: d.available, name: d.name || "",
    default_model: d.default_model, g1: d.g1, calibration: d.calibration,
    log: d.log || [], started_at: d.started_at, returncode: d.returncode,
  });
  if (!model.value) model.value = d.default_model || "";
  if (d.status === "running") {
    startedAt.value = d.started_at ? d.started_at * 1000 : Date.now();
    ensurePolling(true);
  } else {
    ensurePolling(false);
  }
}

function ensurePolling(on) {
  if (on && !timer) timer = setInterval(fetchStatus, 1500);
  if (!on && timer) { clearInterval(timer); timer = null; }
}

async function launch() {
  try {
    const res = await fetch("/api/gpu-backing/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: model.value, mode: mode.value }),
    });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t);
    }
    await fetchStatus();
  } catch (e) {
    s.log = [...s.log, `[ERROR] ${e.message}`];
    s.status = "error";
    s.returncode = "-";
  }
}

/* ---------------- derived presentation ---------------- */

const statusText = computed(() => ({
  idle: "待机", running: "运行中", done: "已完成", error: "失败",
}[s.status] || s.status));

const termLines = computed(() => (s.log || []).slice(-7));
const lastLines = computed(() => (s.log || []).slice(-5));

function kindOf(line) {
  if (/OOM|Error|Traceback|error/i.test(line)) return "bad";
  if (/verdict|CONSISTENT|written|fit:|GPU:/i.test(line)) return "good";
  if (/^N=/.test(line)) return "scan";
  return "";
}

const stage = computed(() => {
  const log = s.log || [];
  const nLines = log.filter((l) => /^N=\s*\d+/.test(l));
  let label = "初始化 CUDA 子进程", pct = 6, steps = 0;
  if (log.some((l) => /loading/i.test(l))) { label = "加载本地模型权重到显存"; pct = 14; }
  if (nLines.length) {
    const last = nLines[nLines.length - 1];
    const n = (last.match(/N=\s*(\d+)/) || [])[1];
    label = `G1 并发扫描中 · N=${n}（${nLines.length}/9 档）`;
    pct = 14 + Math.round((nLines.length / 9) * 56);
    steps = nLines.length;
  }
  if (log.some((l) => /verdict/i.test(l))) { label = "G1 对照完成，写入结果"; pct = 74; steps = 9; }
  if (log.some((l) => /calibration/i.test(l))) { label = "代价参数校准中（prefill/decode 拟合）"; pct = 86; }
  if (log.some((l) => /fit:/.test(l))) { label = "校准完成，生成图表"; pct = 96; }
  return { label, pct, steps };
});

const elapsed = computed(() => {
  if (!startedAt.value) return 0;
  return Math.max(0, Math.round(((timer ? Date.now() : tickNow.value) - startedAt.value) / 1000));
});

/* ---------------- sparkline geometry ---------------- */
const W = 280, H = 104, PL = 30, PR = 272, PT = 12, PB = 92;
const chartData = computed(() => {
  const recs = s.g1?.records || [];
  const ns = [...new Set(recs.map((r) => r.n))].sort((a, b) => a - b);
  const vram = s.g1?.dedicated_vram_gb || null;
  const vals = recs.map((r) => r.peak_mem_gb).filter((v) => v != null);
  if (vram) vals.push(vram);
  const maxV = Math.max(...vals, 1) * 1.08;
  const x = (n) => PL + ((n - ns[0]) / (ns[ns.length - 1] - ns[0])) * (PR - PL);
  const y = (v) => PB - (v / maxV) * (PB - PT);
  const pts = (engine) => recs
    .filter((r) => r.engine === engine && r.peak_mem_gb != null)
    .map((r) => ({ n: r.n, x: x(r.n), y: y(r.peak_mem_gb) }));
  const am = pts("static_prealloc"), te = pts("dynamic_ondemand");
  const oom = recs
    .filter((r) => r.oom || r.vram_exceeded)
    .map((r) => ({ n: r.n, engine: r.engine, x: x(r.n), y: PT + 4 }));
  const tealArea = te.length
    ? `${PL},${PB} ` + te.map((p) => `${p.x},${p.y}`).join(" ") + ` ${PR},${PB}`
    : "";
  const vramY = vram ? y(vram * 0.98) : null;
  return {
    amberPts: am.map((p) => `${p.x},${p.y}`).join(" "),
    tealPts: te.map((p) => `${p.x},${p.y}`).join(" "),
    amberPtsArr: am, tealPtsArr: te, oomMarks: oom, tealArea, vramY,
  };
});
const amberPts = computed(() => chartData.value.amberPts);
const tealPts = computed(() => chartData.value.tealPts);
const amberPtsArr = computed(() => chartData.value.amberPtsArr);
const tealPtsArr = computed(() => chartData.value.tealPtsArr);
const oomMarks = computed(() => chartData.value.oomMarks);
const tealArea = computed(() => chartData.value.tealArea);
const vramY = computed(() => chartData.value.vramY);

const pf = computed(() => {
  const f = s.calibration?.measured?.prefill_fit_total_ms_vs_len || {};
  return { fixed: f.fixed_overhead_ms ?? "—",
           slope: f.marginal_ms_per_token ?? "—", r2: f.r2 ?? "—" };
});
const dc = computed(() => {
  const f = s.calibration?.measured?.decode_fit_ms_vs_batch || {};
  return { fixed: f.fixed_overhead_ms ?? "—",
           slope: f.marginal_ms_per_extra_batch ?? "—", r2: f.r2 ?? "—" };
});
const kvTok = computed(() => {
  const v = s.calibration?.measured?.kv_bytes_per_token_b1;
  return v ? Number(v).toLocaleString() : "—";
});
const shortModel = computed(() => {
  const m = s.g1?.environment?.model || model.value || "";
  const parts = m.split(/[\\/]/);
  return parts[parts.length - 1] || m;
});

onMounted(async () => {
  try { await fetchStatus(); } catch { /* backend may still be starting */ }
});
onBeforeUnmount(() => ensurePolling(false));
</script>

<style scoped>
.gp-panel {
  position: relative;
  margin-top: 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background:
    radial-gradient(900px 200px at 80% -40%, rgba(52, 224, 192, .08), transparent 60%),
    var(--bg2);
  padding: 18px 20px 20px;
  overflow: hidden;
  transition: border-color .3s, box-shadow .3s;
}
.gp-panel.running {
  border-color: rgba(52, 224, 192, .55);
  box-shadow: 0 0 0 1px rgba(52, 224, 192, .15), 0 12px 40px -18px rgba(52, 224, 192, .35);
}
.gp-panel.disabled { opacity: .62; }

/* ---------- header + animated GPU ---------- */
.gp-head { display: flex; align-items: center; gap: 14px; }
.gp-gpu {
  position: relative; width: 54px; height: 54px; flex: none;
  display: grid; place-items: center;
  color: var(--teal);
}
.gp-die { position: relative; z-index: 2; filter: drop-shadow(0 0 6px rgba(52,224,192,.5)); }
.gp-ring {
  position: absolute; inset: 0; border-radius: 50%;
  border: 1px solid rgba(52, 224, 192, .5); opacity: 0;
}
.gp-panel.running .gp-ring { animation: gp-ring 2.2s ease-out infinite; }
.gp-panel.running .gp-ring.r2 { animation-delay: 1.1s; }
@keyframes gp-ring {
  0% { transform: scale(.7); opacity: .9; }
  100% { transform: scale(1.75); opacity: 0; }
}
.gp-cores {
  position: absolute; inset: 0; display: grid; place-items: center;
  grid-template-columns: repeat(3, 5px); grid-template-rows: repeat(3, 5px);
  gap: 4px; z-index: 1; pointer-events: none;
}
.gp-cores i {
  width: 5px; height: 5px; border-radius: 1px;
  background: rgba(52, 224, 192, .9); opacity: .25;
}
.gp-panel.running .gp-cores i { animation: gp-core 1.4s ease-in-out infinite; }
@keyframes gp-core {
  0%, 100% { opacity: .2; transform: scale(.8); }
  50% { opacity: 1; transform: scale(1.25); box-shadow: 0 0 6px rgba(52,224,192,.9); }
}
.gp-panel:not(.running) .gp-gpu { color: #5d7186; }
.gp-panel.running .gp-die { animation: gp-float 2.4s ease-in-out infinite; }
@keyframes gp-float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }

.gp-title { font-weight: 600; font-size: 15px; display: flex; align-items: center; gap: 10px; }
.gp-badge {
  font-size: 10.5px; letter-spacing: .08em; padding: 2px 9px; border-radius: 999px;
  border: 1px solid var(--line); color: #8fa0b5; background: rgba(255,255,255,.03);
}
.gp-badge.running { color: var(--teal); border-color: rgba(52,224,192,.5);
  background: rgba(52,224,192,.1); animation: gp-badge-pulse 1.4s ease-in-out infinite; }
.gp-badge.done { color: var(--teal); border-color: rgba(52,224,192,.45); background: rgba(52,224,192,.08); }
.gp-badge.error { color: #ff8a98; border-color: rgba(255,95,114,.5); background: rgba(255,95,114,.1); }
@keyframes gp-badge-pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(52,224,192,.35); } 50% { box-shadow: 0 0 0 5px rgba(52,224,192,0); } }
.gp-sub { margin-top: 3px; font-size: 12px; color: #7c8aa0; }

/* ---------- body ---------- */
.gp-body { margin-top: 14px; }
.gp-desc { font-size: 13px; line-height: 1.75; color: #b7c4d4; margin: 0 0 12px; }
.gp-desc em { color: #8fa0b5; font-style: normal; }
.gp-adv { margin-bottom: 12px; border: 1px dashed var(--line); border-radius: 8px; padding: 8px 12px; }
.gp-adv summary { cursor: pointer; font-size: 12px; color: #8fa0b5; user-select: none; }
.gp-adv label { display: block; margin: 9px 0 3px; font-size: 12px; color: #9fb0c4; }
.gp-input {
  width: 100%; margin-top: 4px; background: var(--bg1); border: 1px solid var(--line);
  border-radius: 7px; color: var(--ink); padding: 7px 10px; font-size: 12.5px;
}
.gp-input:focus { outline: none; border-color: rgba(52,224,192,.6); }

.gp-run {
  display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
  margin-top: 4px; padding: 10px 20px; border-radius: 9px; border: none;
  font-size: 13.5px; font-weight: 600; color: #041210;
  background: linear-gradient(135deg, #34e0c0, #22c3a2);
  box-shadow: 0 6px 22px -8px rgba(52,224,192,.6);
  transition: transform .15s, box-shadow .2s, filter .2s;
}
.gp-run:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 10px 28px -8px rgba(52,224,192,.75); }
.gp-run:active:not(:disabled) { transform: translateY(0); }
.gp-run:disabled { filter: grayscale(.6) brightness(.7); cursor: not-allowed; }
.gp-run.ghost { background: transparent; color: var(--teal);
  border: 1px solid rgba(52,224,192,.5); box-shadow: none; }
.gp-run.ghost:hover { background: rgba(52,224,192,.08); }
.gp-bolt { font-style: normal; }

/* ---------- running: stage + progress + terminal ---------- */
.gp-stage { display: flex; align-items: center; gap: 10px; font-size: 13px; color: #cfe0ef; margin-bottom: 12px; }
.gp-stage-dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--teal);
  box-shadow: 0 0 10px rgba(52,224,192,.9); animation: gp-blink 1s ease-in-out infinite;
}
@keyframes gp-blink { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
.gp-dots i {
  display: inline-block; width: 4px; height: 4px; margin-left: 3px; border-radius: 50%;
  background: var(--teal); animation: gp-dot 1.2s infinite;
}
.gp-dots i:nth-child(2) { animation-delay: .2s; }
.gp-dots i:nth-child(3) { animation-delay: .4s; }
@keyframes gp-dot { 0%, 60%,100% { opacity: .2; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }

.gp-track { position: relative; height: 8px; border-radius: 99px; background: var(--bg1);
  border: 1px solid var(--line); overflow: visible; margin-bottom: 14px; }
.gp-fill {
  position: relative; height: 100%; border-radius: 99px; overflow: hidden;
  background: linear-gradient(90deg, #1b8f7c, var(--teal));
  box-shadow: 0 0 12px rgba(52,224,192,.55);
  transition: width .7s cubic-bezier(.22,1,.36,1);
}
.gp-shimmer {
  position: absolute; inset: 0;
  background: linear-gradient(100deg, transparent 20%, rgba(255,255,255,.45) 50%, transparent 80%);
  transform: translateX(-100%); animation: gp-shimmer 1.3s ease-in-out infinite;
}
@keyframes gp-shimmer { to { transform: translateX(100%); } }
.gp-ticks { position: absolute; inset: 0; display: flex; justify-content: space-between;
  align-items: center; padding: 0 6px; pointer-events: none; }
.gp-ticks i { width: 3px; height: 3px; border-radius: 50%; background: rgba(255,255,255,.25); transition: background .3s, box-shadow .3s; }
.gp-ticks i.on { background: #dffefa; box-shadow: 0 0 5px rgba(255,255,255,.8); }

.gp-term {
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  background: #060a10;
}
.gp-term.collapsed { margin-top: 12px; max-height: 150px; overflow-y: auto; }
.gp-term-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 12px; font-size: 11.5px; color: #7c8aa0;
  background: rgba(255,255,255,.03); border-bottom: 1px solid var(--line);
}
.gp-tl { color: #ff5f57; font-size: 9px; }
.gp-pid { margin-left: auto; color: var(--teal); }
.gp-lines { padding: 10px 12px; min-height: 92px; max-height: 200px; overflow-y: auto; }
.gp-line { font-size: 11.5px; line-height: 1.7; color: #9fb0c4; white-space: pre-wrap; word-break: break-all; }
.gp-line.scan { color: var(--teal); }
.gp-line.good { color: #8fe6d4; }
.gp-line.bad { color: #ff8a98; }
.gpline-enter-active { transition: all .35s cubic-bezier(.22,1,.36,1); }
.gpline-enter-from { opacity: 0; transform: translateX(-10px); }
.gpline-move { transition: transform .35s; }

/* ---------- result ---------- */
.gp-error {
  margin-bottom: 12px; padding: 9px 13px; border-radius: 9px; font-size: 12.5px;
  color: #ffb3bd; background: rgba(255,95,114,.09); border: 1px solid rgba(255,95,114,.35);
  animation: gp-shake .45s ease;
}
@keyframes gp-shake {
  0%,100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); }
}
.gp-verdict {
  display: flex; align-items: center; gap: 12px; padding: 12px 15px; border-radius: 11px;
  margin-bottom: 14px; animation: gp-pop .5s cubic-bezier(.22,1.4,.4,1);
}
.gp-verdict.ok {
  background: linear-gradient(90deg, rgba(52,224,192,.13), rgba(52,224,192,.03));
  border: 1px solid rgba(52,224,192,.4);
}
.gp-verdict.bad { background: rgba(255,95,114,.08); border: 1px solid rgba(255,95,114,.4); }
@keyframes gp-pop { 0% { opacity: 0; transform: scale(.96) translateY(6px); } 100% { opacity: 1; transform: none; } }
.gp-check {
  width: 34px; height: 34px; flex: none; border-radius: 50%; display: grid; place-items: center;
  font-size: 18px; font-weight: 700;
}
.gp-verdict.ok .gp-check { background: rgba(52,224,192,.18); color: var(--teal);
  box-shadow: 0 0 16px rgba(52,224,192,.4); }
.gp-verdict.bad .gp-check { background: rgba(255,95,114,.18); color: #ff8a98; }
.gp-v-kicker { font-size: 11px; letter-spacing: .06em; color: var(--teal); }
.gp-verdict.bad .gp-v-kicker { color: #ff8a98; }
.gp-v-text { font-size: 13px; color: #d5e2ef; margin-top: 2px; }

.gp-stats { display: flex; align-items: stretch; gap: 10px; margin-bottom: 14px; }
.gp-stat {
  flex: 1; border-radius: 10px; padding: 10px 14px;
  border: 1px solid var(--line); background: rgba(255,255,255,.02);
  animation: gp-pop .5s cubic-bezier(.22,1.4,.4,1) both;
}
.gp-stat:nth-child(3) { animation-delay: .06s; }
.gp-stat:nth-child(5) { animation-delay: .12s; }
.gp-stat span { display: block; font-size: 11px; color: #8fa0b5; margin-bottom: 4px; }
.gp-stat b { font-size: 24px; font-weight: 700; }
.gp-stat.amber b { color: var(--amber); }
.gp-stat.teal b { color: var(--teal); }
.gp-stat.gain { flex: 0 0 92px; display: flex; flex-direction: column; justify-content: center;
  background: rgba(52,224,192,.07); border-color: rgba(52,224,192,.3); }
.gp-stat.gain b { color: var(--teal); font-size: 20px; }
.gp-vs { align-self: center; color: #5d7186; font-size: 15px; }

.gp-spark { width: 100%; height: auto; display: block; }
.gp-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 11px; color: #8fa0b5; margin: 6px 0 12px; }
.gp-legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.gp-x { position: relative; background: transparent !important; }
.gp-x::before, .gp-x::after { content: ""; position: absolute; left: 3px; top: 1px;
  width: 2px; height: 8px; background: #ff5f72; }
.gp-x::before { transform: rotate(45deg); }
.gp-x::after { transform: rotate(-45deg); }

.gp-calib { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.gp-chip {
  font-size: 11.5px; color: #b7c4d4; padding: 6px 11px; border-radius: 999px;
  border: 1px solid var(--line); background: rgba(255,255,255,.02);
  animation: gp-pop .5s .15s cubic-bezier(.22,1.4,.4,1) both;
}
.gp-chip b { color: #d5e2ef; font-weight: 600; margin: 0 2px; }
.gp-chip em { color: #8fa0b5; font-style: normal; margin-left: 7px; }
.gp-env { font-size: 11px; color: #69788c; }
.gp-empty { font-size: 12.5px; color: #7c8aa0; padding: 8px 0; }

@media (prefers-reduced-motion: reduce) {
  .gp-ring, .gp-cores i, .gp-die, .gp-badge, .gp-stage-dot, .gp-dots i,
  .gp-shimmer, .gp-verdict, .gp-stat, .gp-chip, .gp-error { animation: none !important; }
}
</style>
