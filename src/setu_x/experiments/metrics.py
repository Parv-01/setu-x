"""Per-run metrics computed from raw observation rows (docs/methodology.md §4).

Rules enforced here:
  * Only measurement-phase rows count; warm-up rows are reported separately and never mixed in.
  * Percentiles use linear interpolation between order statistics (Hyndman & Fan type 7, the
    NumPy default). A percentile q is reported only if the run has at least 10 / (1 - q)
    samples, i.e. at least 10 observations lie above it: p90 needs n >= 100, p95 n >= 200,
    p99 n >= 1000. Otherwise the value is None and the reason is "insufficient samples".
  * Latency statistics are computed separately for successful requests (outcome confirmed or
    rejected) and for all requests that got an HTTP response. Client timeouts and dropped
    requests have no latency and are counted, not imputed.
  * Nothing here averages percentiles across runs; cross-run aggregation is in statistics.py.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from setu_x.experiments.observation import AVAILABLE_OUTCOMES, FAILED_OUTCOMES, OK_OUTCOMES

PERCENTILES = (50, 90, 95, 99)


def min_samples(q: float) -> int:
    """Smallest n for which percentile q (0-100) has at least 10 observations above it."""
    if q <= 50:
        return 1
    return math.ceil(10 / (1 - q / 100.0) - 1e-9)


def percentile(values: list[float], q: float) -> float:
    """Type-7 (linear interpolation) percentile of a non-empty list; q in [0, 100]."""
    if not values:
        raise ValueError("percentile of an empty list")
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    xs = sorted(values)
    h = (len(xs) - 1) * q / 100.0
    lo = math.floor(h)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def latency_stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        out.update(mean=None, min=None, max=None, stdev=None,
                   **{f"p{q}": None for q in PERCENTILES}, insufficient=[f"p{q}" for q in PERCENTILES])
        return out
    mean = sum(values) / n
    out["mean"] = round(mean, 3)
    out["min"] = round(min(values), 3)
    out["max"] = round(max(values), 3)
    out["stdev"] = round(math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)), 3) if n > 1 else None
    insufficient = []
    for q in PERCENTILES:
        if n >= min_samples(q):
            out[f"p{q}"] = round(percentile(values, q), 3)
        else:
            out[f"p{q}"] = None
            insufficient.append(f"p{q}")
    out["insufficient"] = insufficient
    return out


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return None if v is None or v == "" else float(v)


def run_metrics(rows: list[dict[str, Any]], duration_s: float, faults: list[dict[str, Any]] | None = None,
                recovery: dict[str, float] | None = None) -> dict[str, Any]:
    """Metrics for ONE run. `rows` may include warm-up rows; they are separated here."""
    measure = [r for r in rows if r["phase"] == "measure"]
    warmup = [r for r in rows if r["phase"] == "warmup"]
    outcomes = Counter(r["outcome"] for r in measure)
    statuses = Counter(str(r["http_status"]) for r in measure if r.get("http_status") not in (None, ""))
    error_types = Counter(r["error_type"] for r in measure if r.get("error_type"))
    n = len(measure)
    ok = sum(outcomes[o] for o in OK_OUTCOMES)
    available = sum(outcomes[o] for o in AVAILABLE_OUTCOMES)
    failed = sum(outcomes[o] for o in FAILED_OUTCOMES)
    dropped = outcomes["dropped"]
    sent = n - dropped

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 6) if b else None

    ok_lat = [_num(r, "latency_ms") for r in measure if r["outcome"] in OK_OUTCOMES]
    resp_lat = [_num(r, "latency_ms") for r in measure if r.get("http_status") not in (None, "")]
    svc_lat = [_num(r, "service_latency_ms") for r in measure if r["outcome"] in OK_OUTCOMES]
    lags = [_num(r, "send_lag_ms") for r in measure if r.get("send_lag_ms") not in (None, "")]
    inv = [_num(r, "inventory_ms") for r in measure
           if r["outcome"] in OK_OUTCOMES and _num(r, "inventory_ms") is not None]
    pri = [_num(r, "pricing_ms") for r in measure if r["outcome"] in OK_OUTCOMES and _num(r, "pricing_ms") is not None]
    injected = [_num(r, "injected_delay_ms") for r in measure
                if r["outcome"] in OK_OUTCOMES and _num(r, "injected_delay_ms") is not None]
    retries = sum(int(_num(r, "retries") or 0) for r in measure)
    fallbacks = sum(1 for r in measure if str(r.get("fallback_used")).lower() == "true")
    rejections = sum(int(_num(r, "breaker_rejections") or 0) for r in measure)
    inv_instances = Counter(r["inventory_instance"] for r in measure if r.get("inventory_instance"))

    out: dict[str, Any] = {
        "measure_rows": n,
        "warmup_rows": len(warmup),
        "sent": sent,
        "outcomes": dict(sorted(outcomes.items())),
        "http_status": dict(sorted(statuses.items())),
        "error_types": dict(sorted(error_types.items())),
        "ok": ok,
        "available": available,
        "failed": failed,
        "generator_dropped": dropped,
        "success_rate": ratio(ok, n),
        "availability": ratio(available, n),
        "failure_rate": ratio(failed, n),
        "offered_rps": round(n / duration_s, 3) if duration_s else None,
        "throughput_ok_rps": round(ok / duration_s, 3) if duration_s else None,
        "latency_ok_ms": latency_stats(ok_lat),
        "latency_responded_ms": latency_stats(resp_lat),
        "service_latency_ok_ms": latency_stats(svc_lat),
        "send_lag_ms": latency_stats(lags),
        "inventory_ms": latency_stats(inv),
        "pricing_ms": latency_stats(pri),
        "injected_delay_ms_mean": round(sum(injected) / len(injected), 3) if injected else None,
        "retries": retries,
        "fallback_requests": fallbacks,
        "breaker_rejections": rejections,
        "inventory_instances": dict(sorted(inv_instances.items())),
    }
    if faults:
        params = recovery or {}
        out["recovery"] = [recovery_time(measure, f, duration_s, **params) for f in faults]
    return out


def recovery_time(rows: list[dict[str, Any]], fault: dict[str, Any], duration_s: float, window_s: float = 1.0,
                  hold_s: float = 3.0, threshold: float = 0.95, step_s: float = 0.1) -> dict[str, Any]:
    """Recovery after one fault, from the measurement-phase rows of one run.

    Time axis: `measure_offset_s`, the scheduled send time relative to the start of the measurement
    phase (an outcome is attributed to the moment the request was issued). baseline = success rate
    of requests sent before the fault started. The rolling success rate at time t is computed over
    requests sent in [t - window_s, t); t is "healthy" when that rate is >= threshold * baseline
    (an empty window is not healthy). t is evaluated on a grid of step_s.

      impacted     some rolling window overlapping the fault was not healthy
      recovery_s   time from fault END to the first t from which every t is healthy for hold_s
                   (0.0 when not impacted; None if that does not happen before the run ends).
                   Because the rate looks back window_s, recovery_s includes up to ~window_s of
                   measurement lag; it is reported with its parameters.
    """
    start = float(fault["at_s"])
    end = start + float(fault["duration_s"])
    pts = [(float(r["measure_offset_s"]), r["outcome"] in OK_OUTCOMES) for r in rows]
    base = {"fault_at_s": start, "fault_end_s": round(end, 3), "window_s": window_s, "hold_s": hold_s,
            "threshold": threshold}
    pre = [ok for t, ok in pts if t < start]
    if not pre:
        return {**base, "baseline_success_rate": None, "impacted": None, "recovery_s": None,
                "note": "no requests before the fault: baseline undefined"}
    baseline = sum(pre) / len(pre)
    target = threshold * baseline

    def healthy(t: float) -> bool:
        w = [ok for x, ok in pts if t - window_s <= x < t]
        return bool(w) and sum(w) / len(w) >= target

    # every rolling window that overlaps the fault: window end t in (start, end + window_s]
    k_first = math.floor(start / step_s + 1e-9) + 1
    k_last = math.floor(min(end + window_s, duration_s) / step_s + 1e-9)
    impacted = any(not healthy(k * step_s) for k in range(k_first, k_last + 1))
    result = {**base, "baseline_success_rate": round(baseline, 6), "impacted": impacted}
    if not impacted:
        return {**result, "recovery_s": 0.0}
    hold_steps = math.ceil(hold_s / step_s - 1e-9)
    last = math.floor(duration_s / step_s + 1e-9)
    run_start: int | None = None
    for k in range(math.ceil(end / step_s - 1e-9), last + 1):
        if healthy(k * step_s):
            run_start = k if run_start is None else run_start
            if k - run_start >= hold_steps:
                return {**result, "recovery_s": round(run_start * step_s - end, 3)}
        else:
            run_start = None
    return {**result, "recovery_s": None, "note": "did not recover (sustained) before the run ended"}
