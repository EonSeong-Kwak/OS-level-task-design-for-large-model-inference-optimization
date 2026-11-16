<template>
  <div class="gantt2">
    <div class="g2-scroll">
      <div class="g2-inner">
        <div class="g2-head">
          <span>t (s)</span><span>迭代</span><span>本拍租户 / 请求</span><span class="r">DMA 停顿</span>
        </div>
        <div v-for="(r, i) in rows" :key="i" class="g2-row" :class="{ stall: r.stall > 0 }">
          <span class="mono t">{{ r.t }}</span>
          <span class="kind" :class="r.kind === 'PrefillTick' ? 'pf' : 'dc'">
            {{ r.kind === "PrefillTick" ? "PREFILL" : "DECODE" }}
          </span>
          <span class="chips">
            <template v-if="r.tenants.length">
              <i v-for="(tn, k) in tenantView(r)" :key="k" class="chip" :style="{ color: tenantColor(tn), borderColor: tenantColor(tn) }">
                {{ tn }}
              </i>
              <b class="rids mono">#{{ (r.rids || []).join(" #") }}</b>
            </template>
            <em v-else class="idle">idle（无就绪请求）</em>
          </span>
          <span class="r mono stallv" :class="{ hot: r.stall > 0 }">{{ r.stall > 0 ? r.stall.toFixed(4) : "—" }}</span>
        </div>
        <p v-if="!rows.length" class="g2-empty">该侧没有调度轨迹（结构性微基准）</p>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  rows: { type: Array, default: () => [] },
});

const PALETTE = ["#34e0c0", "#9a8cff", "#4aa8ff", "#f2a94b", "#3ddc84", "#ff7ab8", "#8294ab"];
function tenantColor(name) {
  if (name === "alpha") return "#34e0c0";
  if (name === "beta") return "#9a8cff";
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function tenantView(r) {
  return [...new Set(r.tenants || [])];
}
</script>
