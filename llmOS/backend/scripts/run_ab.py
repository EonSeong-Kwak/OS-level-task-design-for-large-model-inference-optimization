from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmos.experiments import EXPERIMENTS, run_experiment


def _import_plt():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except ImportError:
        return None


def _ys(series: dict, name: str) -> list[float]:
    pts = series.get(name) or []
    return [float(p[1] if isinstance(p, (list, tuple)) else p) for p in pts]


def save_charts(result: dict, out_dir: Path) -> list[Path]:
    plt = _import_plt()
    if plt is None:
        return []
    charts = result.get("charts") or {}
    written: list[Path] = []
    eid = result["id"]

    # 1) Shared line charts: stall timeline / KV util / KV fragmentation.
    line_key = next(
        (k for k in ("stall_timeline", "kv_util", "kv_frag") if charts.get(k)),
        None,
    )
    if line_key:
        series = charts[line_key]
        fig, ax = plt.subplots(figsize=(7, 3.2))
        for name, label in (("baseline", "baseline"), ("double", "double buffer"), ("optimized", "optimized")):
            ys = _ys(series, name)
            if ys:
                ax.plot(ys, label=label)
        ax.set_title(f"{eid} {line_key}")
        ax.legend()
        fig.tight_layout()
        p = out_dir / f"{eid}.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 2) E6: per-tenant P99 grouped bars.
    if "tenant_p99" in charts:
        c = charts["tenant_p99"]
        import numpy as np

        x = np.arange(len(c["tenants"]))
        w = 0.35
        fig, ax = plt.subplots(figsize=(6, 3.4))
        ax.bar(x - w / 2, c["baseline"], w, label="FCFS")
        ax.bar(x + w / 2, c["optimized"], w, label="FAIR")
        ax.set_xticks(x)
        ax.set_xticklabels(c["tenants"])
        ax.set_ylabel("P99 latency (s)")
        ax.set_title(f"{eid} per-tenant P99 latency")
        ax.legend()
        fig.tight_layout()
        p = out_dir / f"{eid}_tenant_p99.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 3) E9: oversubscription ratio vs virtualization time.
    if "oversub_latency" in charts:
        pts = charts["oversub_latency"]
        fig, ax = plt.subplots(figsize=(6, 3.4))
        ax.plot([float(a) for a, _ in pts], [float(b) for _, b in pts], "o-")
        ax.set_xlabel("oversubscription ratio (vmem / phys)")
        ax.set_ylabel("virtualization time (s)")
        ax.set_title(f"{eid} oversubscription vs latency (Pareto)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        p = out_dir / f"{eid}_pareto.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 4) E7: lookahead x cache_layers ablation grid.
    ablation = (result.get("extra") or {}).get("ablation")
    if ablation:
        cache_vals = sorted({row["cache_layers"] for row in ablation})
        fig, ax = plt.subplots(figsize=(6, 3.4))
        for cl in cache_vals:
            rows = sorted((r for r in ablation if r["cache_layers"] == cl), key=lambda r: r["lookahead"])
            ax.plot(
                [r["lookahead"] for r in rows],
                [r["io_stall_ratio"] for r in rows],
                "o-",
                label=f"cache_layers={cl}",
            )
        ax.set_xlabel("lookahead (layers)")
        ax.set_ylabel("io stall ratio")
        ax.set_title(f"{eid} prefetch ablation")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        p = out_dir / f"{eid}_ablation.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 5) E7: compute vs prefetch overlap timeline (spec 6.4-3).
    if "overlap" in charts:
        ov = charts["overlap"]
        variants = [("serial", "serial (no overlap)"), ("double", "double buffer (lookahead=1)"),
                    ("optimized", "prefetch + layer cache")]
        variants = [(k, lb) for k, lb in variants if ov.get(k)]
        if variants:
            fig, axes = plt.subplots(len(variants), 1, figsize=(8, 1.5 * len(variants) + 1.2), sharex=True)
            axes = [axes] if len(variants) == 1 else list(axes)
            for ax, (key, label) in zip(axes, variants):
                comp = [(s, e - s) for s, e in ov[key].get("compute", [])]
                dma = [(s, e - s) for s, e in ov[key].get("dma", [])]
                if comp:
                    ax.broken_barh(comp, (0.55, 0.35), facecolors="#2f9e8f", label="GPU compute")
                if dma:
                    ax.broken_barh(dma, (0.10, 0.35), facecolors="#c98a2b", label="DMA (NVMe→GPU)")
                ax.set_yticks([0.27, 0.72])
                ax.set_yticklabels(["DMA", "compute"], fontsize=8)
                ax.set_ylim(0, 1)
                ax.set_title(label, fontsize=9, loc="left")
                ax.grid(axis="x", alpha=0.25)
                if key == "serial":
                    ax.legend(fontsize=7, ncol=2, loc="upper right")
            axes[-1].set_xlabel("simulated time (s)")
            fig.suptitle(f"{eid} compute / prefetch overlap", fontsize=10)
            fig.tight_layout()
            p = out_dir / f"{eid}_overlap.png"
            fig.savefig(p)
            plt.close(fig)
            written.append(p)

    # 6) E2: physical blocks, independent copy vs COW.
    if eid == "E2" and "physical_blocks" in (result.get("baseline") or {}):
        base, opt = result["baseline"], result["optimized"]
        fig, ax = plt.subplots(figsize=(5.4, 3.4))
        bars = ax.bar(["independent copy", "COW shared prefix"],
                      [base["physical_blocks"], opt["physical_blocks"]],
                      color=["#c98a2b", "#2f9e8f"], width=0.55)
        for b in bars:
            ax.annotate(f"{int(b.get_height())} blocks", (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=9)
        ax.set_ylabel("physical blocks")
        ax.set_title(f"{eid} {base['branches']} branches: blocks used")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        p = out_dir / f"{eid}_blocks.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 7) E10: per-tenant accumulated service time, 1:1 vs 3:1.
    if eid == "E10" and "alpha" in (result.get("baseline") or {}):
        import numpy as np

        base, opt = result["baseline"], result["optimized"]
        x = np.arange(2)
        w = 0.35
        fig, ax = plt.subplots(figsize=(5.6, 3.4))
        ax.bar(x - w / 2, [base["alpha"], base["beta"]], w, label="1:1 share", color="#c98a2b")
        ax.bar(x + w / 2, [opt["alpha"], opt["beta"]], w, label="3:1 share", color="#2f9e8f")
        ax.set_xticks(x)
        ax.set_xticklabels(["tenant α", "tenant β"])
        ax.set_ylabel("accumulated service time (s)")
        ax.set_title(f"{eid} MPS share 1:1 vs 3:1 (Jain={opt['fairness']:.2f})")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        p = out_dir / f"{eid}_share.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    # 8) E12: max model size with serial load vs prefetch.
    if eid == "E12" and "max_model_gb" in (result.get("baseline") or {}):
        base, opt = result["baseline"], result["optimized"]
        fig, ax = plt.subplots(figsize=(5.4, 3.4))
        bars = ax.bar(["serial full load", "prefetch (lookahead only)"],
                      [base["max_model_gb"], opt["max_model_gb"]],
                      color=["#c98a2b", "#2f9e8f"], width=0.55)
        for b in bars:
            ax.annotate(f"{b.get_height():.0f} GB", (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=9)
        ax.axhline(opt["hbm_gb"], ls="--", lw=0.9, color="#b8453c", label=f"HBM cap {opt['hbm_gb']:.0f} GB")
        ax.set_ylabel("max model size (GB)")
        ax.set_title(f"{eid} single-GPU model size limit")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        p = out_dir / f"{eid}_scale.png"
        fig.savefig(p)
        plt.close(fig)
        written.append(p)

    return written


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "results"
    out_dir.mkdir(exist_ok=True)
    ids = sys.argv[1:] or list(EXPERIMENTS)
    for exp_id in ids:
        result = run_experiment(exp_id)
        slim = dict(result)
        for side in ("baseline", "optimized"):
            if isinstance(slim.get(side), dict):
                slim[side] = {k: v for k, v in slim[side].items() if k not in {"gantt", "series"}}
        slim.pop("gantt", None)
        path = out_dir / f"{exp_id.upper()}.json"
        path.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        pngs = save_charts(result, out_dir)
        base = slim.get("baseline", {})
        opt = slim.get("optimized", {})
        print(f"== {result['id']} {result['title']}")
        print("digest   :", result.get("config_digest"))
        print("baseline :", {k: base[k] for k in list(base)[:8]})
        print("optimized:", {k: opt[k] for k in list(opt)[:8]})
        print("verdict  :", result.get("verdict"))
        print("wrote    :", path)
        for p in pngs:
            print("chart    :", p)


if __name__ == "__main__":
    main()
