"""Cross-run statistics (docs/methodology.md §5).

The run is the unit of replication. Each run is reduced to run-level statistics (e.g. its median
latency, its success rate); conditions are compared on those, never on pooled requests treated as
independent samples, and never by averaging per-run percentiles.

Confidence intervals: percentile bootstrap over runs (resampling runs with replacement), seeded so
results are reproducible. With the small run counts typical here (3-10) the bootstrap interval is
coarse and tends to be too narrow; it is reported together with n and the raw per-run values so
the reader can judge it. No p-values are produced.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from statistics import median
from typing import Any

from setu_x.experiments.metrics import latency_stats, percentile

DEFAULT_B = 2000


def bootstrap_ci(values: Sequence[float], stat: Callable[[Sequence[float]], float] = median, b: int = DEFAULT_B,
                 seed: int = 0, level: float = 0.95) -> tuple[float, float] | None:
    """Percentile-bootstrap CI of stat(values) over runs. None if fewer than 2 values."""
    xs = [float(v) for v in values if v is not None]
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    boots = sorted(stat([rng.choice(xs) for _ in xs]) for _ in range(b))
    alpha = (1 - level) / 2
    return round(percentile(boots, 100 * alpha), 6), round(percentile(boots, 100 * (1 - alpha)), 6)


def bootstrap_diff_ci(a: Sequence[float], b_values: Sequence[float], stat: Callable[[Sequence[float]], float] = median,
                      b: int = DEFAULT_B, seed: int = 0, level: float = 0.95) -> tuple[float, float] | None:
    """CI of stat(a) - stat(b), resampling runs within each condition independently."""
    xa = [float(v) for v in a if v is not None]
    xb = [float(v) for v in b_values if v is not None]
    if len(xa) < 2 or len(xb) < 2:
        return None
    rng = random.Random(seed)
    diffs = sorted(stat([rng.choice(xa) for _ in xa]) - stat([rng.choice(xb) for _ in xb]) for _ in range(b))
    alpha = (1 - level) / 2
    return round(percentile(diffs, 100 * alpha), 6), round(percentile(diffs, 100 * (1 - alpha)), 6)


# run-level statistics extracted from run_metrics() output: name -> path
RUN_LEVEL = {
    "median_latency_ok_ms": ("latency_ok_ms", "p50"),
    "p90_latency_ok_ms": ("latency_ok_ms", "p90"),
    "p95_latency_ok_ms": ("latency_ok_ms", "p95"),
    "p99_latency_ok_ms": ("latency_ok_ms", "p99"),
    "mean_latency_ok_ms": ("latency_ok_ms", "mean"),
    "success_rate": ("success_rate",),
    "availability": ("availability",),
    "throughput_ok_rps": ("throughput_ok_rps",),
    "median_inventory_ms": ("inventory_ms", "p50"),
    "median_pricing_ms": ("pricing_ms", "p50"),
    "retries": ("retries",),
    "fallback_requests": ("fallback_requests",),
    "breaker_rejections": ("breaker_rejections",),
    "generator_dropped": ("generator_dropped",),
}


def _get(d: dict, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def condition_summary(run_metrics: list[dict[str, Any]], pooled_ok_latencies: list[float], seed: int = 0,
                      b: int = DEFAULT_B) -> dict[str, Any]:
    """Aggregate the runs of one condition."""
    out: dict[str, Any] = {"runs": len(run_metrics), "run_level": {}}
    for name, path in RUN_LEVEL.items():
        values = [_get(m, path) for m in run_metrics]
        present = [float(v) for v in values if v is not None]
        entry: dict[str, Any] = {"per_run": values, "n": len(present)}
        if present:
            entry["median"] = round(median(present), 6)
            entry["min"] = round(min(present), 6)
            entry["max"] = round(max(present), 6)
            ci = bootstrap_ci(present, median, b, seed)
            entry["median_ci95"] = list(ci) if ci else None
        else:
            entry.update(median=None, min=None, max=None, median_ci95=None)
        if len(present) < len(values):
            entry["note"] = "some runs had too few samples for this statistic"
        out["run_level"][name] = entry
    recov = [r.get("recovery_s") for m in run_metrics for r in (m.get("recovery") or [])]
    if recov:
        present = [float(v) for v in recov if v is not None]
        out["run_level"]["recovery_s"] = {
            "per_run": recov, "n": len(present),
            "median": round(median(present), 6) if present else None,
            "median_ci95": (list(ci) if (ci := bootstrap_ci(present, median, b, seed)) else None),
            "not_recovered": sum(1 for v in recov if v is None),
        }
    out["pooled_latency_ok_ms"] = {**latency_stats(pooled_ok_latencies),
                                   "label": "POOLED across runs: descriptive only, requests are not independent"}
    return out


def compare(conditions: dict[str, dict[str, Any]], reference: str, metrics: Sequence[str] = (
        "median_latency_ok_ms", "success_rate", "availability"), seed: int = 0, b: int = DEFAULT_B) -> list[dict]:
    """Difference of run-level medians (condition - reference) with a bootstrap CI over runs."""
    out = []
    ref = conditions[reference]["run_level"]
    for name, summ in conditions.items():
        if name == reference:
            continue
        for metric in metrics:
            a = [v for v in summ["run_level"][metric]["per_run"] if v is not None]
            r = [v for v in ref[metric]["per_run"] if v is not None]
            diff = (median(a) - median(r)) if a and r else None
            ci = bootstrap_diff_ci(a, r, median, b, seed)
            out.append({"condition": name, "reference": reference, "metric": metric,
                        "difference_of_medians": round(diff, 6) if diff is not None else None,
                        "ci95": list(ci) if ci else None, "n_condition": len(a), "n_reference": len(r)})
    return out
