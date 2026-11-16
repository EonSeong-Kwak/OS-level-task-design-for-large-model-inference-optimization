<template>
  <div class="bc">
    <svg
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="xMidYMid meet"
      @mousemove="onMove"
      @mouseleave="hover = null"
    >
      <g v-for="(tick, i) in ticks" :key="'g' + i">
        <line :x1="pad.l" :x2="W - pad.r" :y1="yOf(tick)" :y2="yOf(tick)" class="grid" :class="{ base: i === ticks.length - 1 }" />
        <text :x="pad.l - 8" :y="yOf(tick) + 3.5" text-anchor="end" class="axis">{{ fmt(tick) }}</text>
      </g>

      <g v-for="(lab, ci) in labels" :key="ci">
        <rect
          v-for="(g, gi) in groups"
          :key="gi"
          :x="cx(ci) - slotW / 2 + gi * barW + 2"
          :y="yOf(val(g, ci))"
          :width="barW - 4"
          :height="Math.max(0, H - pad.b - yOf(val(g, ci)))"
          :fill="g.color"
          rx="2"
          :opacity="hover && hover.ci !== ci ? 0.42 : 1"
        >
          <title>{{ g.name }} · {{ lab }}: {{ fmt(val(g, ci)) }}</title>
        </rect>
        <text :x="cx(ci)" :y="H - 6" text-anchor="middle" class="axis cat">{{ lab }}</text>
      </g>
    </svg>

    <div
      v-if="hover"
      class="bc-tip mono"
      :style="{ left: hover.pct + '%', transform: hover.pct > 55 ? 'translateX(-104%)' : 'translateX(10px)' }"
    >
      <div class="tip-x">{{ hover.label }}</div>
      <div v-for="it in hover.items" :key="it.name" class="tip-row">
        <i :style="{ background: it.color }"></i>
        <span>{{ it.name }}</span>
        <b>{{ it.text }}</b>
      </div>
    </div>

    <div class="bc-legend">
      <span v-for="g in groups" :key="g.name" class="lc-item">
        <i :style="{ background: g.color }"></i>{{ g.name }}
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  labels: { type: Array, default: () => [] },
  // [{ name, color, values: [] }]
  groups: { type: Array, default: () => [] },
  format: { type: String, default: "num3" },
});

const W = 660;
const H = 232;
const pad = { l: 52, r: 14, t: 12, b: 24 };

function val(g, ci) {
  return Number(g.values[ci] ?? 0);
}
const hi = computed(() => {
  let m = 0;
  props.groups.forEach((g) => g.values.forEach((v) => (m = Math.max(m, Number(v) || 0))));
  return m <= 0 ? 1 : m * 1.12;
});
const ticks = computed(() => [0, 1, 2, 3, 4].map((i) => (hi.value * i) / 4));
function yOf(v) {
  return H - pad.b - (v / hi.value) * (H - pad.t - pad.b);
}
const slotW = computed(() => (W - pad.l - pad.r) / Math.max(1, props.labels.length));
const barW = computed(() => Math.min(46, slotW.value / Math.max(1, props.groups.length)));
function cx(ci) {
  return pad.l + slotW.value * (ci + 0.5);
}
function fmt(v) {
  const a = Math.abs(v);
  if (props.format === "pct") return (v * 100).toFixed(0) + "%";
  if (props.format === "int") return Math.round(v).toString();
  if (props.format === "f1") return v.toFixed(1);
  if (props.format === "f2") return v.toFixed(2);
  return a >= 10 ? v.toFixed(1) : v.toFixed(3);
}

/* ---------------- hover ---------------- */
const hover = ref(null);
function onMove(ev) {
  const rect = ev.currentTarget.getBoundingClientRect();
  if (!rect.width || !props.labels.length) return;
  const px = (ev.clientX - rect.left) * (W / rect.width);
  const ci = Math.floor((px - pad.l) / slotW.value);
  if (ci < 0 || ci >= props.labels.length) { hover.value = null; return; }
  hover.value = {
    ci,
    pct: (cx(ci) / W) * 100,
    label: props.labels[ci],
    items: props.groups.map((g) => ({ name: g.name, color: g.color, text: fmt(val(g, ci)) })),
  };
}
</script>
