from llmos.experiments import run_experiment


def _tp(result: dict) -> float:
    return float(result["optimized"].get("throughput", result["optimized"].get("io_stall_ratio", 0.0)))


def test_golden_w1_e4_repeatable():
    a = run_experiment("E4", seed=42)
    b = run_experiment("E4", seed=42)
    ta, tb = _tp(a), _tp(b)
    if ta == 0 and tb == 0:
        return
    assert abs(ta - tb) / max(abs(ta), 1e-9) <= 0.01


def test_e1_e7_e9_have_digest_and_two_columns():
    for exp_id in ("E1", "E7", "E9"):
        r = run_experiment(exp_id, seed=42)
        assert r.get("config_digest")
        assert "baseline" in r and "optimized" in r
