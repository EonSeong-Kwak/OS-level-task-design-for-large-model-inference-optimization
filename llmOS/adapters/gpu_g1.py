"""GPU reality-check adapter for llmOS (tech-plan §3.3 / §6.3, docs/系统设计文档.md §7).

This module is deliberately DECOUPLED from the simulator: it never imports `llmos`.
It runs the same E1-direction comparison on a real engine:

  G1  StaticCache  (KV reserved up-front for every slot, ~contiguous pre-alloc)
      vs DynamicCache (KV grows on demand, ~paged allocation proxy; vLLM is the
      full paged engine on Colab, see doc §2)

  CALIBRATION  measured prefill ms/token, decode ms/step, KV bytes/token on the
      real GPU, printed next to the FROZEN constants in backend/configs/default.yaml
      (sensitivity appendix only — the frozen config is never rewritten).

Runs unmodified on a local Windows GPU and on Google Colab T4:
  python gpu_g1.py --model "D:\\Python\\data\\Qwen2.5-0.5B"
  python gpu_g1.py --model Qwen/Qwen2.5-0.5B-Instruct          # Colab
  python gpu_g1.py --model <model> --mode calib
"""

import argparse
import gc
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone

import torch

DEFAULT_NS = [64, 96, 128, 160, 192, 256, 320, 512, 640]
PROMPT_LEN = 32      # actual W1 short-dialog prompt
MAX_NEW_TOKENS = 32  # actual generation
RESERVED_BUDGET = 2048  # max_tokens pre-reserved per slot (W2 long-context cap)


# ---------------------------------------------------------------- utilities

def env_block(model_name, dtype):
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "python": platform.python_version(),
        "transformers": _transformers_version(),
        "model": model_name,
        "dtype": str(dtype).replace("torch.", ""),
    }


def _transformers_version():
    try:
        import transformers
        return transformers.__version__
    except Exception:
        return None


def reset_cuda():
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()


def peak_gb():
    return round(torch.cuda.max_memory_allocated() / 1e9, 3)


def load_model(model_name, dtype):
    from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
    tok = AutoTokenizer.from_pretrained(model_name)
    config = AutoConfig.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype).to("cuda")
    model.eval()
    return model, tok, config


def kv_cache_bytes(cache):
    """Sum bytes of every resident K/V tensor in a Dynamic/Static cache."""
    total = 0
    layers = getattr(cache, "layers", None)
    if layers:
        for layer in layers:
            if isinstance(layer, (tuple, list)):
                tensors = layer[:2]
            else:  # transformers>=5 StaticLayer/DynamicLayer: .keys / .values
                tensors = (getattr(layer, "keys", None),
                           getattr(layer, "values", None))
            for t in tensors:
                if isinstance(t, torch.Tensor):
                    total += t.nelement() * t.element_size()
    elif hasattr(cache, "to_legacy_cache"):
        for k, v in cache.to_legacy_cache():
            total += k.nelement() * k.element_size()
            total += v.nelement() * v.element_size()
    return total


def dedicated_vram_gb():
    """Dedicated VRAM via nvidia-smi (WDDM otherwise spills to host 'shared'
    memory without raising CUDA OOM, so 8 GB must be an explicit threshold)."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return round(float(out.stdout.strip().splitlines()[0]) / 1024, 2)
    except Exception:
        return None


# ---------------------------------------------------------------- G1 trial

def run_static_trial(model, n, total_len, prompt_len):
    """StaticCache: full KV for `total_len` reserved on first forward (baseline)."""
    from transformers import StaticCache
    reset_cuda()
    cache = StaticCache(model.config, max_cache_len=total_len)
    ids = torch.randint(0, model.config.vocab_size, (n, prompt_len), device="cuda")
    pos = torch.arange(prompt_len, device="cuda")
    torch.cuda.synchronize()
    t0 = time.time()
    with torch.no_grad():
        model(ids, past_key_values=cache, cache_position=pos, use_cache=True)
        torch.cuda.synchronize()
    elapsed = time.time() - t0
    return elapsed, peak_gb(), kv_cache_bytes(cache)


def run_dynamic_trial(model, n, prompt_len, gen_steps):
    """DynamicCache: KV grows per generated token (on-demand / paged proxy)."""
    from transformers import DynamicCache
    reset_cuda()
    cache = DynamicCache()
    ids = torch.randint(0, model.config.vocab_size, (n, prompt_len), device="cuda")
    torch.cuda.synchronize()
    t0 = time.time()
    with torch.no_grad():
        out = model(ids, past_key_values=cache, use_cache=True)
        next_ids = out.logits[:, -1].argmax(-1, keepdim=True)
        for _ in range(gen_steps):
            out = model(next_ids, past_key_values=cache, use_cache=True)
            next_ids = out.logits[:, -1].argmax(-1, keepdim=True)
        torch.cuda.synchronize()
    elapsed = time.time() - t0
    return elapsed, peak_gb(), kv_cache_bytes(cache)


def trial_record(engine, n, fn):
    try:
        elapsed, peak, kv_bytes = fn()
        return {"engine": engine, "n": n, "ok": True, "oom": False,
                "elapsed_s": round(elapsed, 3), "peak_mem_gb": peak,
                "kv_cache_gb": round(kv_bytes / 1e9, 3), "notes": ""}
    except torch.cuda.OutOfMemoryError as e:
        return {"engine": engine, "n": n, "ok": False, "oom": True,
                "elapsed_s": None, "peak_mem_gb": peak_gb(),
                "kv_cache_gb": None, "notes": str(e)[:200]}


def mode_g1(model, tok, config, dtype, args):
    actual_len = args.prompt + args.gen
    vram_gb = dedicated_vram_gb()
    ns = args.ns or DEFAULT_NS
    records = []
    for n in ns:
        records.append(trial_record(
            "static_prealloc", n,
            lambda n=n: run_static_trial(model, n, args.budget, args.prompt)))
        records.append(trial_record(
            "dynamic_ondemand", n,
            lambda n=n: run_dynamic_trial(model, n, args.prompt, args.gen)))
        reset_cuda()
        s, d = records[-2], records[-1]
        print(f"N={n:>3}  static(reserve {args.budget}): "
              f"{'OOM' if s['oom'] else str(s['peak_mem_gb'])+'GB'}"
              f"   dynamic(actual {actual_len}): "
              f"{'OOM' if d['oom'] else str(d['peak_mem_gb'])+'GB'}")

    # On Windows WDDM, over-VRAM allocations spill to host shared memory without
    # a CUDA OOM; the real "fits on the GPU" boundary is dedicated VRAM.
    for r in records:
        r["vram_exceeded"] = bool(
            vram_gb and (not r["ok"] or r["peak_mem_gb"] is None
                         or r["peak_mem_gb"] > vram_gb * 0.98))

    def fits(r):
        return r["ok"] and not r["vram_exceeded"]

    def max_ok(engine):
        ok = [r["n"] for r in records if r["engine"] == engine and fits(r)]
        return max(ok) if ok else 0

    s_max, d_max = max_ok("static_prealloc"), max_ok("dynamic_ondemand")
    verdict = "CONSISTENT_WITH_SIM_E1" if d_max >= s_max else "INCONSISTENT"
    payload = {
        "experiment": "G1",
        "maps_to": "E1",
        "prompt_len_actual": args.prompt,
        "max_new_tokens_actual": args.gen,
        "reserved_budget_per_seq": args.budget,
        "reserve_over_actual_x": round(args.budget / actual_len, 1),
        "environment": env_block(args.model, dtype),
        "dedicated_vram_gb": vram_gb,
        "fit_rule": "ok and peak_mem_gb <= 0.98 * dedicated VRAM "
                    "(WDDM host-shared spill counts as NOT fit)",
        "max_concurrency": {"static_prealloc": s_max, "dynamic_ondemand": d_max},
        "concurrency_gain_x": round(d_max / s_max, 2) if s_max else None,
        "direction_rule": "dynamic max N >= static max N (or static OOMs first)",
        "verdict": verdict,
        "records": records,
    }
    _dump_json(payload, args.outdir, "g1.json")
    _plot_g1(payload, args.outdir)
    print(f"G1 verdict: {verdict}  (static max N={s_max}, dynamic max N={d_max})")
    return payload


def _plot_g1(payload, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ns = sorted({r["n"] for r in payload["records"]})
    def series(engine, field):
        m = {r["n"]: r[field] for r in payload["records"] if r["engine"] == engine}
        return [m.get(n) for n in ns]
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=130)
    ax.plot(ns, series("static_prealloc", "peak_mem_gb"), "o-",
            color="#f2a94b",
            label=f"StaticCache pre-alloc {payload['reserved_budget_per_seq']} tok/slot")
    ax.plot(ns, series("dynamic_ondemand", "peak_mem_gb"), "s-",
            color="#34e0c0",
            label=f"DynamicCache on-demand ({payload['prompt_len_actual']}+"
                  f"{payload['max_new_tokens_actual']} tok)")
    oom_y = max([v for v in series("static_prealloc", "peak_mem_gb") +
                 series("dynamic_ondemand", "peak_mem_gb") if v] + [8]) - 0.3
    for r in payload["records"]:
        if r["oom"] or r.get("vram_exceeded"):
            ax.scatter([r["n"]], [oom_y], marker="x", color="#ff5d5d", s=90, zorder=5)
    ax.scatter([], [], marker="x", color="#ff5d5d", label="OOM / VRAM spill")
    if payload.get("dedicated_vram_gb"):
        ax.axhline(payload["dedicated_vram_gb"] * 0.98, ls=":",
                   color="#8a97a8", lw=1)
        ax.text(ns[0], payload["dedicated_vram_gb"] * 0.98 + 0.15,
                f"dedicated VRAM {payload['dedicated_vram_gb']} GB",
                color="#8a97a8", fontsize=8)
    ax.set_xlabel("concurrent sequences N")
    ax.set_ylabel("peak GPU memory (GB)")
    ax.set_title("G1 / E1: reserve-by-max vs on-demand KV (Qwen2.5-0.5B, fp16)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "g1.png"))
    plt.close(fig)


# ---------------------------------------------------------------- calibration

def timed(fn, reps, warmups):
    for _ in range(warmups):
        fn()
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(reps):
        fn()
    torch.cuda.synchronize()
    return (time.time() - t0) / reps


def mode_calib(model, tok, config, dtype, args):
    # prefill: total wall time T(L) = fixed launch overhead c + marginal rate r*L.
    # A raw ms/token ratio is meaningless when c dominates (tiny eager model,
    # B=1 on a desktop GPU), so c and r are fitted over long prompts.
    prefill = []
    for L in [64, 128, 256, 512, 1024, 2048]:
        ids = torch.randint(0, config.vocab_size, (1, L), device="cuda")
        pos = torch.arange(L, device="cuda")

        def run():
            with torch.inference_mode():
                model(ids, cache_position=pos, use_cache=False)
        s = timed(run, reps=5, warmups=3)
        prefill.append({"prompt_len": L, "total_ms": round(s * 1000, 3),
                        "apparent_ms_per_token": round(s / L * 1000, 4)})
    pf_fit = _linear_fit([p["prompt_len"] for p in prefill],
                         [p["total_ms"] for p in prefill])

    # decode: ms/step vs batch, fit intercept (launch floor) + per-batch slope
    decode, kv_bytes_per_token = [], None
    for B in [1, 2, 4, 8, 16, 32]:
        ids = torch.randint(0, config.vocab_size, (B, 512), device="cuda")
        with torch.inference_mode():
            out = model(ids, use_cache=True)
            kv = out.past_key_values
            if B == 1:
                kv_bytes_per_token = kv_cache_bytes(kv) / (B * 512)
        nxt = torch.randint(0, config.vocab_size, (B, 1), device="cuda")

        def run():
            with torch.inference_mode():
                model(nxt, past_key_values=kv, use_cache=True)
        s = timed(run, reps=20, warmups=5)
        decode.append({"batch": B, "ms_per_step": round(s * 1000, 4)})
        del kv, out
        reset_cuda()
    dc_fit = _linear_fit([d["batch"] for d in decode],
                         [d["ms_per_step"] for d in decode])

    frozen = _read_frozen_constants(args.sim_config)
    payload = {
        "experiment": "CALIBRATION",
        "note": "Sensitivity appendix only; frozen sim constants are not rewritten "
                "(tech-plan §4.3.1). Timing on a tiny eager model on a desktop GPU "
                "is launch-overhead bound; use fitted marginal rates, not the raw "
                "apparent ms/token ratio.",
        "environment": env_block(args.model, dtype),
        "measured": {
            "prefill": prefill,
            "prefill_fit_total_ms_vs_len": {
                "fixed_overhead_ms": pf_fit["intercept"],
                "marginal_ms_per_token": pf_fit["slope"],
                "r2": pf_fit["r2"],
            },
            "decode": decode,
            "decode_fit_ms_vs_batch": {
                "fixed_overhead_ms": dc_fit["intercept"],
                "marginal_ms_per_extra_batch": dc_fit["slope"],
                "r2": dc_fit["r2"],
            },
            "kv_bytes_per_token_b1": kv_bytes_per_token,
            "n_layers": getattr(config, "num_hidden_layers", None),
            "n_kv_heads": getattr(config, "num_key_value_heads", None),
            "head_dim": getattr(config, "hidden_size", 0) //
                       getattr(config, "num_attention_heads", 1),
        },
        "frozen_sim_constants": frozen,
    }
    _dump_json(payload, args.outdir, "calibration.json")
    _plot_calib(payload, args.outdir)
    print("calibration written:", os.path.join(args.outdir, "calibration.json"))
    print(f"  prefill fit: fixed={pf_fit['intercept']}ms "
          f"marginal={pf_fit['slope']}ms/token R2={pf_fit['r2']}")
    print(f"  decode  fit: fixed={dc_fit['intercept']}ms "
          f"slope={dc_fit['slope']}ms/batch R2={dc_fit['r2']}")
    return payload


def _linear_fit(xs, ys):
    """Least-squares y = intercept + slope*x with R^2 (stdlib only)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return {"slope": round(slope, 5), "intercept": round(intercept, 3),
            "r2": round(r2, 4)}


def _read_frozen_constants(path):
    if not path or not os.path.exists(path):
        return None
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return {k: cfg.get(k) for k in
                ["alpha_prefill", "beta_decode", "block_size", "kv_block_gb"]
                if k in cfg}
    except Exception:
        return None


def _plot_calib(payload, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    m = payload["measured"]
    fr = payload["frozen_sim_constants"] or {}
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 3.8), dpi=130)

    # left: prefill TOTAL ms vs prompt length + LS fit
    pf = m["prefill"]
    xs = [p["prompt_len"] for p in pf]
    ys = [p["total_ms"] for p in pf]
    fit = m["prefill_fit_total_ms_vs_len"]
    ax1.plot(xs, ys, "o", color="#9a8cff", label="measured total")
    ax1.plot(xs, [fit["fixed_overhead_ms"] + fit["marginal_ms_per_token"] * x
                  for x in xs], "-", color="#9a8cff", alpha=0.5,
             label=f"fit: {fit['fixed_overhead_ms']}ms + "
                   f"{fit['marginal_ms_per_token']}ms/tok (R²={fit['r2']})")
    if fr.get("alpha_prefill"):
        ax1.plot(xs, [fr["alpha_prefill"] * 1000 * x for x in xs], "--",
                 color="#f2a94b",
                 label=f"frozen alpha={fr['alpha_prefill']*1000:.3f} ms/tok")
    ax1.set_xlabel("prompt length (B=1)"); ax1.set_ylabel("prefill total ms")
    ax1.set_title("Prefill: total time = overhead + rate·L")
    ax1.grid(alpha=0.3); ax1.legend(fontsize=7.5)

    # right: decode ms/step vs batch + LS fit
    dc = m["decode"]
    bs = [d["batch"] for d in dc]
    dfit = m["decode_fit_ms_vs_batch"]
    ax2.plot(bs, [d["ms_per_step"] for d in dc], "s", color="#34e0c0",
             label="measured")
    ax2.plot(bs, [dfit["fixed_overhead_ms"] + dfit["marginal_ms_per_extra_batch"] * b
                  for b in bs], "-", color="#34e0c0", alpha=0.5,
             label=f"fit: {dfit['fixed_overhead_ms']}ms + "
                   f"{dfit['marginal_ms_per_extra_batch']}ms/B")
    if fr.get("beta_decode"):
        ax2.plot(bs, [fr["beta_decode"] * 1000 * b for b in bs], "--",
                 color="#f2a94b",
                 label=f"frozen beta={fr['beta_decode']*1000:.2f} ms·B")
    ax2.set_xlabel("batch size B"); ax2.set_ylabel("decode ms/step")
    ax2.set_title("Decode: step time = overhead + slope·B")
    ax2.grid(alpha=0.3); ax2.legend(fontsize=7.5)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "calibration.png"))
    plt.close(fig)


def _dump_json(payload, outdir, name):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="llmOS local/Colab GPU reality-check")
    ap.add_argument("--model", default=os.environ.get("LLMOS_GPU_MODEL",
                        "Qwen/Qwen2.5-0.5B-Instruct"),
                    help="local path or HF model id")
    ap.add_argument("--mode", choices=["g1", "calib", "all"], default="all")
    ap.add_argument("--gen", type=int, default=MAX_NEW_TOKENS,
                    help="actual generated tokens (on-demand arm)")
    ap.add_argument("--prompt", type=int, default=PROMPT_LEN)
    ap.add_argument("--budget", type=int, default=RESERVED_BUDGET,
                    help="max_tokens KV pre-reserved per slot by StaticCache")
    ap.add_argument("--ns", type=int, nargs="*", default=None)
    ap.add_argument("--outdir", default=None,
                    help="default: <repo>/gpu_results")
    ap.add_argument("--sim-config", default=None,
                    help="default.yaml for frozen-constant comparison")
    args = ap.parse_args()

    if not torch.cuda.is_available():
        sys.exit("CUDA is required for the GPU reality-check.")

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    args.outdir = args.outdir or os.path.join(repo, "gpu_results")
    args.sim_config = args.sim_config or os.path.join(
        repo, "backend", "configs", "default.yaml")

    dtype = torch.float16
    print(f"loading {args.model} ({dtype}) ...")
    model, tok, config = load_model(args.model, dtype)
    print(f"GPU: {torch.cuda.get_device_name(0)}  "
          f"weights={torch.cuda.memory_allocated()/1e9:.2f} GB")

    if args.mode in ("g1", "all"):
        mode_g1(model, tok, config, dtype, args)
    if args.mode in ("calib", "all"):
        mode_calib(model, tok, config, dtype, args)


if __name__ == "__main__":
    main()
