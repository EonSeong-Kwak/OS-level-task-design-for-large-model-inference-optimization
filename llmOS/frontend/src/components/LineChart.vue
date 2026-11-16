<template>
  <div class="lc">
    <svg
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="xMidYMid meet"
      @mousemove="onMove"
      @mouseleave="hover = null"
    >
      <!-- grid + y labels -->
      <g v-for="(tick, i) in ticks" :key="'g' + i">
        <line
          :x1="pad.l" :x2="W - pad.r"
          :y1="yOf(tick)" :y2="yOf(tick)"
          class="grid"
          :class="{ base: i === ticks.length - 1 }"
        />
        <text :x="pad.l - 8" :y="yOf(tick) + 3.5" text-anchor="end" class="axis">{{ fmt(tick) }}</text>
      </g>
      <!-- x labels (first / mid / last) -->
      <text v-if="xLabels[0] !== null" :x="pad.l" :y="H - 6" class="axis">{{ xLabels[0] }}</text>
      <text v-if="xLabels[1] !== null" :x="(pad.l + W - pad.r) / 2" :y="H - 6" text-anchor="middle" class="axis">{{ xLabels[1] }}</text>
      <text v-if="xLabels[2] !== null" :x="W - pad.r" :y="H - 6" text-anchor="end" class="axis">{{ xLabels[2] }}</text>

      <!-- series -->
      <g v-for="(s, si) in normSeries" :key="si">
        <polygon v-if="s.area" :points="s.area" class="area" :fill="s.color" />
        <polyline
          :points="s.points"
          fill="none"
          :stroke="s.color"
          :stroke-width="s.width || 1.8"
          :stroke-dasharray="s.dash"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
      </g>

      <!-- hover crosshair + points -->
      <g v-if="hover">
        <line :x1="hover.x" :x2="hover.x" :y1="pad.t" :y2="H - pad.b" class="cursor" />
        <circle v-for="(d, i) in hover.dots" :key="i" :cx="d[0]" :cy="d[1]" r="3.2" :fill="d[2]" class="hdot" />
      </g>
    </svg>

    <div
      v-if="hover"
      class="lc-tip mono"
      :style="{ left: hover.pct + '%', transform: hover.pct > 55 ? 'translateX(-104%)' : 'translateX(10px)' }"
    >
      <div class="tip-x">{{ hover.xlabel }}</div>
      <div v-for="it in hover.items" :key="it.name" class="tip-row">
        <i :style="{ background: it.color }"></i>
        <span>{{ it.name }}</span>
        <b>{{ it.text }}</b>
      </div>
    </div>

    <div v-if="legend.length" class="lc-legend">
      <span v-for="l in legend" :key="l.name" class="lc-item">
        <i :style="{ background: l.color }" :class="{ dashed: l.dash }"></i>{{ l.name }}
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  // [{ name, color, points: [[x,y]...] | [y...], dash, area, width, legend }]
  series: { type: Array, default: () => [] },
  format: { type: String, default: "num2" }, // pct | num3 | num2 | num1 | int
  xformat: { type: String, default: "num3" },
  xCustom: { type: Array, default: null }, // [lo, mid, hi] raw x labels override
});

const W = 660;
const H = 232;
const pad = { l: 52, r: 14, t: 12, b: 22 };

function valueOf(p) {
  return Array.isArray(p) ? Number(p[1]) : Number(p);
}
function xOf(p, i) {
  return Array.isArray(p) ? Number(p[0]) : i;
}

const scales = computed(() => {
  let lo = Infinity, hi = -Infinity, xlo = Infinity, xhi = -Infinity, n = 0;
  for (const s of props.series) {
    s.points.forEach((p, i) => {
      const y = valueOf(p);
      const x = xOf(p, i);
      if (Number.isFinite(y)) { lo = Math.min(lo, y); hi = Math.max(hi, y); }
      if (Number.isFinite(x)) { xlo = Math.min(xlo, x); xhi = Math.max(xhi, x); }
      n = Math.max(n, s.points.length);
    });
  }
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  if (hi - lo < 1e-12) { hi = lo + 1; }
  const padv = (hi - lo) * 0.08;
  lo = props.format === "pct" ? Math.min(lo, 0) : lo - padv;
  hi = hi + padv;
  return { lo, hi, xlo: Number.isFinite(xlo) ? xlo : 0, xhi: Number.isFinite(xhi) ? xhi : 1, n };
});

const ticks = computed(() => {
  const { lo, hi } = scales.value;
  return [0, 1, 2, 3, 4].map((i) => lo + ((hi - lo) * i) / 4);
});

function fmt(v) {
  const a = Math.abs(v);
  if (props.format === "pct") return (v * 100).toFixed(0) + "%";
  if (props.format === "int") return Math.round(v).toString();
  if (props.format === "f1") return v.toFixed(1);
  if (props.format === "f2") return v.toFixed(2);
  if (props.format === "num3") return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  if (props.format === "num1") return v.toFixed(1);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}
function xfmt(v) {
  if (props.xformat === "int") return Math.round(v).toString();
  return Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(3);
}

function yOf(v) {
  const { lo, hi } = scales.value;
  return H - pad.b - ((v - lo) / (hi - lo || 1)) * (H - pad.t - pad.b);
}
function xPos(x) {
  const { xlo, xhi, n } = scales.value;
  if (n <= 1) return pad.l;
  if (xhi - xlo < 1e-12) return pad.l;
  return pad.l + ((x - xlo) / (xhi - xlo)) * (W - pad.l - pad.r);
}

const normSeries = computed(() =>
  props.series.map((s) => {
    const xy = [], vals = [], xvals = [];
    s.points.forEach((p, i) => {
      const y = valueOf(p);
      const x = xOf(p, i);
      vals.push(y);
      xvals.push(x);
      xy.push([xPos(x), yOf(y)]);
    });
    const line = xy.map((p) => p.join(",")).join(" ");
    const area = s.area && xy.length > 1
      ? `${pad.l},${H - pad.b} ${line} ${W - pad.r},${H - pad.b}`
      : null;
    return { ...s, xy, vals, xvals, points: line, area };
  })
);

const xLabels = computed(() => {
  if (props.xCustom) return props.xCustom;
  const { n, xlo, xhi } = scales.value;
  if (n < 2) return [null, null, null];
  return [xfmt(xlo), xfmt((xlo + xhi) / 2), xfmt(xhi)];
});

const legend = computed(() => props.series.filter((s) => s.legend !== false).map((s) => ({ name: s.name, color: s.color, dash: s.dash })));

/* ---------------- hover ---------------- */
const hover = ref(null);
function onMove(ev) {
  const rect = ev.currentTarget.getBoundingClientRect();
  if (!rect.width) return;
  const px = (ev.clientX - rect.left) * (W / rect.width);
  let refS = null;
  for (const s of normSeries.value) {
    if (!refS || s.xy.length > refS.xy.length) refS = s;
  }
  if (!refS || !refS.xy.length) return;
  let bi = 0, bd = Infinity;
  refS.xy.forEach((p, i) => {
    const d = Math.abs(p[0] - px);
    if (d < bd) { bd = d; bi = i; }
  });
  const dots = [], items = [];
  for (const s of normSeries.value) {
    if (!s.xy.length) continue;
    const j = Math.min(bi, s.xy.length - 1);
    dots.push([s.xy[j][0], s.xy[j][1], s.color]);
    items.push({ name: s.name, color: s.color, text: fmt(s.vals[j]) });
  }
  hover.value = {
    x: refS.xy[bi][0],
    pct: (refS.xy[bi][0] / W) * 100,
    xlabel: xfmt(refS.xvals[bi]),
    items,
    dots,
  };
}
</script>
