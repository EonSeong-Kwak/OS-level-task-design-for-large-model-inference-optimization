<template>
  <div class="layout">
    <aside>
      <div class="brand">
        <h1>llmOS</h1>
        <p>推理操作系统评测台</p>
      </div>
      <nav>
        <button
          v-for="item in catalog"
          :key="item.id"
          :class="{ active: selected === item.id }"
          @click="selected = item.id"
        >
          <span class="eid">{{ item.id }}</span>
          <span>{{ item.title }}</span>
        </button>
      </nav>
      <div class="aside-foot">
        后端 {{ health ? "在线" : "未连接" }} · 历史记录入库
      </div>
    </aside>

    <main>
      <header class="top">
        <div>
          <h2>{{ currentTitle }}</h2>
          <p>同一 seed 下基线 vs 优化，结果写入 MySQL <code>llmos.experiment_runs</code></p>
        </div>
        <div class="actions">
          <label>seed <input v-model.number="seed" type="number" min="1" /></label>
          <button class="primary" :disabled="running" @click="runSelected">
            {{ running ? "运行中…" : "运行实验" }}
          </button>
        </div>
      </header>

      <p v-if="error" class="error">{{ error }}</p>

      <section v-if="result" class="cards">
        <article>
          <h3>基线</h3>
          <pre>{{ pretty(result.baseline) }}</pre>
        </article>
        <article>
          <h3>优化</h3>
          <pre>{{ pretty(result.optimized) }}</pre>
        </article>
      </section>

      <section v-if="result" class="verdict">
        <strong>结论</strong>
        <span>{{ result.verdict }}</span>
      </section>

      <section v-if="chartPairs.length" class="charts">
        <div v-for="c in chartPairs" :key="c.name" class="chart">
          <h3>{{ c.name }}</h3>
          <svg viewBox="0 0 640 220" preserveAspectRatio="none">
            <polyline :points="c.base" fill="none" stroke="#8a6d3b" stroke-width="2" />
            <polyline :points="c.opt" fill="none" stroke="#1f4e79" stroke-width="2" />
          </svg>
          <div class="legend">
            <span class="base">基线</span>
            <span class="opt">优化</span>
          </div>
        </div>
      </section>

      <section v-if="ganttRows.length" class="gantt-wrap">
        <h3>调度甘特（优化侧，最近迭代）</h3>
        <div class="gantt">
          <div v-for="(row, i) in ganttRows" :key="i" class="gantt-row">
            <i>{{ row.t }}</i>
            <b v-for="rid in row.rids" :key="rid">#{{ rid }}</b>
          </div>
        </div>
      </section>

      <section class="history">
        <h3>历史运行</h3>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>实验</th>
              <th>seed</th>
              <th>时间</th>
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in runs" :key="r.run_id" @click="openRun(r.run_id)">
              <td>{{ r.run_id }}</td>
              <td>{{ r.exp_id }} {{ r.title }}</td>
              <td>{{ r.seed }}</td>
              <td>{{ r.created_at }}</td>
              <td>{{ r.verdict }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";

const catalog = ref([]);
const selected = ref("E1");
const seed = ref(42);
const running = ref(false);
const health = ref(false);
const error = ref("");
const result = ref(null);
const runs = ref([]);

const currentTitle = computed(() => {
  const hit = catalog.value.find((x) => x.id === selected.value);
  return hit ? `${hit.id} · ${hit.title}` : "选择实验";
});

const chartPairs = computed(() => {
  const charts = result.value?.charts || {};
  return Object.entries(charts).map(([name, series]) => ({
    name,
    base: toPoints(series.baseline || []),
    opt: toPoints(series.optimized || series.baseline || []),
  }));
});

const ganttRows = computed(() => {
  const g = result.value?.gantt?.optimized || result.value?.gantt?.baseline || [];
  return g.slice(-24).map((row) => ({
    t: Number(row.t).toFixed(4),
    rids: row.rids || [],
  }));
});

function toPoints(series) {
  if (!series.length) return "";
  const ys = series.map((p) => (Array.isArray(p) ? Number(p[1]) : Number(p)));
  const max = Math.max(...ys, 1e-9);
  const min = Math.min(...ys, 0);
  const span = max - min || 1;
  return series
    .map((p, i) => {
      const x = (i / Math.max(series.length - 1, 1)) * 640;
      const yv = Array.isArray(p) ? Number(p[1]) : Number(p);
      const y = 200 - ((yv - min) / span) * 180;
      return `${x},${y}`;
    })
    .join(" ");
}

function pretty(obj) {
  const skip = new Set(["gantt", "series"]);
  const slim = {};
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (!skip.has(k) && typeof v !== "object") slim[k] = v;
    else if (!skip.has(k) && v && typeof v === "object" && !Array.isArray(v)) slim[k] = v;
  });
  return JSON.stringify(slim, null, 2);
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
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
    catalog.value = (await api("/api/experiments")).items;
    runs.value = (await api("/api/runs")).items;
  } catch (e) {
    health.value = false;
    error.value = "无法连接后端，请先启动 FastAPI :8000";
  }
}

async function runSelected() {
  running.value = true;
  error.value = "";
  try {
    const data = await api("/api/experiments/run", {
      method: "POST",
      body: JSON.stringify({ exp_id: selected.value, seed: seed.value }),
    });
    result.value = data.result;
    await refresh();
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    running.value = false;
  }
}

async function openRun(id) {
  const data = await api(`/api/runs/${id}`);
  selected.value = data.exp_id;
  result.value = data.result;
}

onMounted(refresh);
</script>
