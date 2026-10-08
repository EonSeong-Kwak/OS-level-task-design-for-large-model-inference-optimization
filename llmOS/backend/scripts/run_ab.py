from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmos.experiments import EXPERIMENTS, run_experiment


def _save_png(result: dict, path: Path) -> None:
    charts = result.get("charts") or {}
    series = None
    for key in ("stall_timeline", "kv_util", "kv_frag"):
        if key in charts and (charts[key].get("baseline") or charts[key].get("optimized")):
            series = charts[key]
            title = key
            break
    if not series:
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    def ys(name: str) -> list[float]:
        pts = series.get(name) or []
        out = []
        for p in pts:
            out.append(float(p[1] if isinstance(p, (list, tuple)) else p))
        return out

    fig, ax = plt.subplots(figsize=(7, 3.2))
    if ys("baseline"):
        ax.plot(ys("baseline"), label="baseline")
    if ys("double"):
        ax.plot(ys("double"), label="double")
    if ys("optimized"):
        ax.plot(ys("optimized"), label="optimized")
    ax.set_title(f"{result['id']} {title}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


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
        png = out_dir / f"{exp_id.upper()}.png"
        _save_png(result, png)
        base = slim.get("baseline", {})
        opt = slim.get("optimized", {})
        print(f"== {result['id']} {result['title']}")
        print("digest   :", result.get("config_digest"))
        print("baseline :", {k: base[k] for k in list(base)[:8]})
        print("optimized:", {k: opt[k] for k in list(opt)[:8]})
        print("verdict  :", result.get("verdict"))
        print("wrote    :", path)


if __name__ == "__main__":
    main()
