"""Metric definitions (docs/methodology.md §4)."""
from __future__ import annotations

import pytest

from setu_x.experiments.metrics import latency_stats, min_samples, percentile, recovery_time, run_metrics


def test_percentile_type7_known_values():
    # NumPy's default ('linear', Hyndman & Fan type 7) gives these values
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 90) == pytest.approx(3.7)
    assert percentile([15, 20, 35, 40, 50], 40) == pytest.approx(29.0)
    assert percentile([7], 99) == 7
    assert percentile([3, 1, 2], 0) == 1 and percentile([3, 1, 2], 100) == 3


def test_percentile_rejects_bad_input():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_min_samples_rule_ten_observations_above():
    assert [min_samples(q) for q in (50, 90, 95, 99)] == [1, 100, 200, 1000]


def test_latency_stats_marks_insufficient_percentiles():
    s = latency_stats([float(i) for i in range(199)])
    assert s["p90"] is not None and s["p95"] is None and s["p99"] is None
    assert s["insufficient"] == ["p95", "p99"]
    s = latency_stats([float(i) for i in range(1000)])
    assert s["p99"] == pytest.approx(percentile([float(i) for i in range(1000)], 99), abs=1e-3)
    assert s["insufficient"] == []
    empty = latency_stats([])
    assert empty["n"] == 0 and empty["p50"] is None and empty["mean"] is None


def row(seq, outcome, t, latency=10.0, phase=None, **kw):
    status = {"confirmed": 200, "degraded": 200, "rejected": 409, "unavailable": 503, "error": 500}.get(outcome)
    phase = phase or ("measure" if t >= 0 else "warmup")
    sent = outcome != "dropped"
    return {"seq": seq, "phase": phase, "outcome": outcome, "measure_offset_s": t, "scheduled_offset_s": t + 1,
            "http_status": status, "latency_ms": latency if sent and outcome != "client_timeout" else None,
            "service_latency_ms": latency if sent else None, "send_lag_ms": 0.1 if sent else None,
            "error_type": None, "retries": kw.get("retries", 0), "fallback_used": kw.get("fallback", False),
            "breaker_rejections": 0, "inventory_ms": 4.0, "pricing_ms": 5.0, "injected_delay_ms": 0.0,
            "inventory_instance": "inventory-a"}


def test_run_metrics_counts_rates_and_excludes_warmup():
    rows = [row(0, "confirmed", -0.5, latency=999.0)]  # warm-up: must not count
    rows += [row(i, "confirmed", i * 0.1, latency=10.0 + i) for i in range(1, 7)]
    rows += [row(7, "rejected", 0.7), row(8, "degraded", 0.8), row(9, "unavailable", 0.9),
             row(10, "client_timeout", 1.0), row(11, "dropped", 1.1)]
    m = run_metrics(rows, duration_s=2.0)
    assert m["warmup_rows"] == 1 and m["measure_rows"] == 11
    assert m["ok"] == 7 and m["available"] == 8 and m["failed"] == 2 and m["generator_dropped"] == 1
    assert m["sent"] == 10
    # denominators include dropped requests (offered load), so dropping cannot inflate success
    assert m["success_rate"] == pytest.approx(7 / 11, abs=1e-6)
    assert m["availability"] == pytest.approx(8 / 11, abs=1e-6)
    assert m["throughput_ok_rps"] == pytest.approx(3.5)
    assert m["offered_rps"] == pytest.approx(5.5)
    assert m["latency_ok_ms"]["n"] == 7 and m["latency_ok_ms"]["max"] < 999
    assert m["http_status"] == {"200": 7, "409": 1, "503": 1}
    assert m["outcomes"]["dropped"] == 1


def series(fail_from, fail_to, total_s=10.0, rate=20):
    out = []
    n = int(total_s * rate)
    for i in range(n):
        t = i / rate
        out.append(row(i, "unavailable" if fail_from <= t < fail_to else "confirmed", t))
    return out


def test_recovery_not_impacted_is_zero():
    r = recovery_time(series(99, 99), {"at_s": 3, "duration_s": 2}, 10.0)
    assert r["impacted"] is False and r["recovery_s"] == 0.0 and r["baseline_success_rate"] == 1.0


def test_recovery_measured_from_fault_end_with_window_lag():
    # failures exactly during the fault [3, 5): the 1 s window is healthy again once >= 95% of it is
    # after 5 s, i.e. from t = 5.95 -> recovery_s ~ 0.95 s (documented window lag)
    r = recovery_time(series(3, 5), {"at_s": 3, "duration_s": 2}, 10.0)
    assert r["impacted"] is True
    assert r["recovery_s"] == pytest.approx(1.0, abs=0.11)


def test_recovery_longer_outage_than_fault():
    r = recovery_time(series(3, 7, total_s=12), {"at_s": 3, "duration_s": 2}, 12.0)
    assert r["recovery_s"] == pytest.approx(3.0, abs=0.11)


def test_recovery_none_when_never_sustained():
    r = recovery_time(series(3, 9.5), {"at_s": 3, "duration_s": 2}, 10.0)
    assert r["recovery_s"] is None and "did not recover" in r["note"]


def test_recovery_needs_baseline():
    r = recovery_time(series(0, 2), {"at_s": 0, "duration_s": 2}, 10.0)
    assert r["baseline_success_rate"] is None and r["recovery_s"] is None


def test_recovery_hold_time_resets_on_relapse():
    rows = series(3, 5, total_s=12)
    for r in rows:  # relapse at 6.5 .. 7.0
        if 6.5 <= r["measure_offset_s"] < 7.0:
            r["outcome"], r["http_status"] = "unavailable", 503
    res = recovery_time(rows, {"at_s": 3, "duration_s": 2}, 12.0, hold_s=3.0)
    assert res["recovery_s"] == pytest.approx(3.0, abs=0.11)  # first sustained healthy stretch starts ~8.0 s


def test_recovery_short_fault_off_grid_is_detected():
    # regression: a fault shorter than the window starting between grid points must still count
    r = recovery_time(series(3.05, 3.55), {"at_s": 3.05, "duration_s": 0.5}, 10.0)
    assert r["impacted"] is True and r["recovery_s"] is not None
