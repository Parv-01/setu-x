"""Cross-run statistics: bootstrap over runs, no averaging of percentiles."""
from __future__ import annotations

from statistics import median

import pytest

from setu_x.experiments.metrics import percentile
from setu_x.experiments.statistics import bootstrap_ci, bootstrap_diff_ci, compare, condition_summary


def test_bootstrap_is_seeded_and_brackets_the_median():
    xs = [10.0, 12.0, 11.0, 13.0, 10.5]
    a = bootstrap_ci(xs, seed=3)
    assert a == bootstrap_ci(xs, seed=3)
    lo, hi = a
    assert lo <= median(xs) <= hi
    assert min(xs) <= lo and hi <= max(xs)


def test_bootstrap_needs_two_runs():
    assert bootstrap_ci([5.0]) is None
    assert bootstrap_diff_ci([1.0, 2.0], [3.0]) is None


def test_bootstrap_constant_values_degenerate_interval():
    assert bootstrap_ci([7.0, 7.0, 7.0]) == (7.0, 7.0)


def test_diff_ci_sign():
    lo, hi = bootstrap_diff_ci([110.0, 112.0, 111.0], [10.0, 11.0, 12.0], seed=1)
    assert 97 <= lo <= hi <= 103


def fake_run(p50, p95, success):
    return {"latency_ok_ms": {"p50": p50, "p90": None, "p95": p95, "p99": None, "mean": p50},
            "success_rate": success, "availability": success, "throughput_ok_rps": 20.0,
            "inventory_ms": {"p50": 1.0}, "pricing_ms": {"p50": 2.0}, "retries": 0, "fallback_requests": 0,
            "breaker_rejections": 0, "generator_dropped": 0}


def test_condition_summary_reports_median_of_run_values_not_mean_of_percentiles():
    runs = [fake_run(10, 20, 1.0), fake_run(11, 90, 1.0), fake_run(30, 25, 0.9)]
    s = condition_summary(runs, pooled_ok_latencies=[1.0, 2.0, 3.0, 4.0])
    p95 = s["run_level"]["p95_latency_ok_ms"]
    assert p95["per_run"] == [20, 90, 25]
    assert p95["median"] == 25          # median of per-run values ...
    assert p95["median"] != (20 + 90 + 25) / 3  # ... never their average
    assert s["run_level"]["p99_latency_ok_ms"]["n"] == 0
    assert s["run_level"]["p99_latency_ok_ms"]["median"] is None
    pooled = s["pooled_latency_ok_ms"]
    assert pooled["p50"] == pytest.approx(percentile([1.0, 2.0, 3.0, 4.0], 50))
    assert "POOLED" in pooled["label"]


def test_compare_against_reference():
    a = condition_summary([fake_run(10, 20, 1.0), fake_run(11, 21, 1.0)], [])
    b = condition_summary([fake_run(60, 70, 0.5), fake_run(61, 71, 0.5)], [])
    res = compare({"ref": a, "slow": b}, "ref", metrics=("median_latency_ok_ms", "success_rate"))
    lat = next(r for r in res if r["metric"] == "median_latency_ok_ms")
    assert lat["difference_of_medians"] == pytest.approx(50.0)
    assert lat["n_condition"] == 2 and lat["n_reference"] == 2
    succ = next(r for r in res if r["metric"] == "success_rate")
    assert succ["difference_of_medians"] == pytest.approx(-0.5)


def test_bootstrap_interval_reflects_spread_between_runs():
    # regression (mutation check): a "bootstrap" that never resamples collapses to a zero-width interval
    lo, hi = bootstrap_ci([float(i) for i in range(1, 11)], seed=0)
    assert lo <= 4.5 and hi >= 6.5 and hi - lo >= 2.0
    narrow = bootstrap_ci([10.0, 10.1, 9.9, 10.05, 9.95], seed=0)
    assert narrow[1] - narrow[0] < 0.25
