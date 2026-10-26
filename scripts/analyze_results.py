#!/usr/bin/env python3
"""Phase 8 analysis: figures and tables from experiment records ONLY.

    python scripts/analyze_results.py results/<id_A> results/<id_B> ... --out results/analysis

For every experiment directory it:
  1. re-validates the records from disk (setu_x.experiments.records.validate_records); a directory
     whose records do not validate is reported and skipped, never plotted;
  2. computes run-level statistics with setu_x.experiments.statistics (the run is the unit of
     replication; percentiles are never averaged; intervals are bootstrap-over-runs);
  3. writes <out>/<scenario_id>.json (all numbers used in the text) and PNG figures.

Hypothesis checks (H1-H5) are computed against the rejection criteria in docs/research_questions.md
and written as data ("supported" / "not supported" / "inconclusive" with the numbers), so the prose
in docs/findings.md can be checked against them. Needs matplotlib (requirements/analysis.lock).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from setu_x.experiments.metrics import percentile  # noqa: E402
from setu_x.experiments.records import validate_records  # noqa: E402
from setu_x.experiments.statistics import bootstrap_ci, bootstrap_diff_ci  # noqa: E402

C1, C2, C3, C4 = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6"   # validated categorical slots
BAD, WARN, MUTED, GRID = "#d03b3b", "#fab219", "#898781", "#e1e0d9"
OK = {"confirmed", "rejected"}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
                     "axes.axisbelow": True, "figure.dpi": 110, "savefig.bbox": "tight"})


# ------------------------------------------------------------------------------------------ loading
class Exp:
    def __init__(self, d: Path) -> None:
        self.dir = d
        self.manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        valid, report = validate_records(d, self.manifest.get("runs", []))
        self.validation = report
        self.rows = valid
        self.id = self.manifest["experiment_id"]
        self.scenario = self.manifest["scenario_id"]
        self.conditions = [c["name"] for c in self.manifest["scenario"]["conditions"]]
        self.per_run = self.summary["per_run"]

    def ok(self) -> bool:
        return bool(self.validation["ok"]) and self.summary.get("status") == "completed"

    def measure(self, run_id: str) -> list[dict]:
        return [r for r in self.rows if r["run_id"] == run_id and r["phase"] == "measure"]

    def runs_of(self, cond: str) -> list[dict]:
        return [m for m in self.per_run if m["condition"] == cond]

    def run_level(self, cond: str, key: str) -> list[float]:
        return [v for v in (self.get(m, key) for m in self.runs_of(cond)) if v is not None]

    @staticmethod
    def get(m: dict, key: str) -> Any:
        d: Any = m
        for k in key.split("."):
            d = d.get(k) if isinstance(d, dict) else None
        return d


def inventory_attempts(exp: Exp, run_id: str) -> tuple[int, int]:
    """(inventory attempts, requests) for one run.

    `attempts` in a row is the sum over both dependencies. In parallel call mode pricing is called
    exactly once per order, and in these scenarios pricing is healthy (no faults, so no retries), so
    inventory attempts = attempts - 1. Breaker rejections are not attempts (no call is made).
    """
    run = next(r for r in exp.manifest["runs"] if r["run_id"] == run_id)
    cond = next(c for c in exp.manifest["scenario"]["conditions"] if c["name"] == run["condition"])
    mode = cond.get("call_mode") or exp.manifest["base_placement"]["call_mode"]
    if mode != "parallel" or any(f["target"] == "pricing" for f in cond.get("faults", [])):
        raise ValueError(f"{run_id}: inventory attempts are only derivable in parallel mode with healthy pricing")
    rows = [r for r in exp.measure(run_id) if r.get("attempts") is not None]
    return sum(int(r["attempts"]) - 1 for r in rows), len(rows)


def stat_block(values: list[float], seed: int) -> dict:
    ci = bootstrap_ci(values, median, seed=seed) if len(values) >= 2 else None
    return {"per_run": [round(v, 4) for v in values], "n_runs": len(values),
            "median": round(median(values), 4) if values else None, "ci95": list(ci) if ci else None}


def diff_block(a: list[float], b: list[float], seed: int) -> dict:
    """median(a) - median(b) with a bootstrap CI over runs."""
    if not a or not b:
        return {"difference": None, "ci95": None}
    ci = bootstrap_diff_ci(a, b, median, seed=seed) if len(a) >= 2 and len(b) >= 2 else None
    return {"difference": round(median(a) - median(b), 4), "ci95": list(ci) if ci else None,
            "n": [len(a), len(b)]}


def save(fig, out: Path, name: str) -> str:
    fig.savefig(out / f"{name}.png")
    plt.close(fig)
    return f"{name}.png"


def caption(ax, exp: Exp) -> None:
    ax.figure.text(0.01, -0.07, f"{exp.id} · {exp.manifest['environment_mode']} mode · commit "
                   f"{exp.manifest['git_commit']} · dots = runs, bar = median of runs, line = 95% bootstrap CI",
                   fontsize=7.5, color=MUTED)


def dotplot(ax, exp: Exp, conds: list[str], key: str, color=C1, offset=0.0, label=None):
    for i, c in enumerate(conds):
        vals = exp.run_level(c, key)
        if not vals:
            continue
        x = i + offset
        n = len(vals)
        xs = [x + (j / (n - 1) - 0.5) * 0.12 if n > 1 else x for j in range(n)]
        ax.scatter(xs, vals, s=22, color=color, edgecolor="white", linewidth=1, zorder=3,
                   label=label if i == 0 else None)
        ci = bootstrap_ci(vals, median, seed=exp.manifest["scenario"]["seed"]) if n >= 2 else None
        if ci:
            ax.plot([x, x], ci, color=color, alpha=0.5, linewidth=2, zorder=2)
        ax.plot([x - 0.14, x + 0.14], [median(vals)] * 2, color=color, linewidth=3, zorder=4)
    ax.set_xticks(range(len(conds)), conds, rotation=20 if len(conds) > 4 else 0)


# ------------------------------------------------------------------------------------------ per experiment
def analyse_A(exp: Exp, out: Path) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    res = {"runs": []}
    fig, ax = plt.subplots(figsize=(7, 3.6))
    colors = [C1, C2, C3, C4, MUTED]
    for k, m in enumerate(exp.per_run):
        lat = sorted(float(r["latency_ms"]) for r in exp.measure(m["run_id"]) if r["outcome"] in OK)
        ys = [(i + 1) / len(lat) for i in range(len(lat))]
        ax.plot(lat, ys, color=colors[k % len(colors)], linewidth=1.6, label=m["run_id"])
        res["runs"].append({"run_id": m["run_id"], "n": len(lat), **{q: m["latency_ok_ms"][q] for q in (
            "p50", "p90", "p95", "p99", "mean")}, "success_rate": m["success_rate"],
            "send_lag_p99_ms": m["send_lag_ms"].get("p99")})
    ax.set_xscale("log")
    ax.set_xlabel("latency of successful requests (ms, log scale)")
    ax.set_ylabel("fraction of requests ≤ x")
    ax.set_title("A · Baseline latency distribution, one curve per run (local mode)")
    ax.legend(fontsize=8, frameon=False)
    caption(ax, exp)
    res["figure"] = save(fig, out, "A_baseline_ecdf")
    for q in ("p50", "p95", "p99"):
        res[f"{q}_across_runs"] = stat_block([r[q] for r in res["runs"] if r[q] is not None], seed)
    res["success_rate"] = stat_block([r["success_rate"] for r in res["runs"]], seed)
    return res


def analyse_B(exp: Exp, out: Path) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    ds = [0, 25, 50, 100]
    res: dict[str, Any] = {"modes": {}}
    fig, ax = plt.subplots(figsize=(7.2, 4))
    for mode, color, off in (("seq", C1, -0.12), ("par", C2, 0.12)):
        base = exp.run_level(f"{mode}-d0", "latency_ok_ms.p50")
        rows = []
        for i, d in enumerate(ds):
            vals = exp.run_level(f"{mode}-d{d}", "latency_ok_ms.p50")
            n = len(vals)
            xs = [i + off + ((j / (n - 1) - 0.5) * 0.08 if n > 1 else 0) for j in range(n)]
            ax.scatter(xs, vals, s=20, color=color, edgecolor="white", linewidth=1, zorder=3,
                       label=("sequential" if mode == "seq" else "parallel") if i == 0 else None)
            if vals:
                ax.plot([i + off - 0.08, i + off + 0.08], [median(vals)] * 2, color=color, linewidth=3)
            shift = diff_block(vals, base, seed)
            rows.append({"d_ms": d, "median_latency": stat_block(vals, seed), "shift_vs_d0": shift})
        res["modes"][mode] = rows
    # configured expectations (not measurements): seq = base + d ; par = base + max(50, d) - 50
    bseq = median(exp.run_level("seq-d0", "latency_ok_ms.p50") or [math.nan])
    bpar = median(exp.run_level("par-d0", "latency_ok_ms.p50") or [math.nan])
    ax.plot(range(4), [bseq + d for d in ds], color=C1, linestyle="--", linewidth=1, alpha=0.7,
            label="sequential: measured d0 + configured d")
    ax.plot(range(4), [bpar + max(50, d) - 50 for d in ds], color=C2, linestyle="--", linewidth=1, alpha=0.7,
            label="parallel: measured d0 + max(50, d) − 50")
    ax.set_xticks(range(4), [f"{d} ms" for d in ds])
    ax.set_xlabel("injected delay on the gateway→pricing link, d (inventory link fixed at 50 ms)")
    ax.set_ylabel("median latency of successful requests (ms)")
    ax.set_title("B · Delay propagation: sequential vs parallel calls (simulated mode)")
    ax.legend(fontsize=8, frameon=False)
    caption(ax, exp)
    res["figure"] = save(fig, out, "B_network_delay")
    # H1 check
    base_spread = exp.run_level("seq-d0", "latency_ok_ms.p50")
    var = (max(base_spread) - min(base_spread)) if len(base_spread) > 1 else None
    checks = []
    for d_row_s, d_row_p in zip(res["modes"]["seq"][1:], res["modes"]["par"][1:], strict=True):
        d = d_row_s["d_ms"]
        s_shift = d_row_s["shift_vs_d0"]["difference"]
        p_shift = d_row_p["shift_vs_d0"]["difference"]
        ci = d_row_s["shift_vs_d0"]["ci95"]
        checks.append({"d_ms": d, "seq_shift": s_shift, "seq_shift_ci95": ci, "par_shift": p_shift,
                       "seq_shift_minus_d": None if s_shift is None else round(s_shift - d, 3),
                       "par_smaller_than_seq": None if None in (s_shift, p_shift) else p_shift < s_shift})
    res["H1"] = {"baseline_run_range_ms": var, "checks": checks,
                 "criterion": "rejected if |seq shift − d| exceeds the run-to-run variability of the baseline "
                              "(95% CI over runs), or the parallel shift is not smaller than the sequential one"}
    return res


def timeline(ax, exp: Exp, run_id: str, title: str):
    rows = exp.measure(run_id)
    secs: dict[int, list[bool]] = {}
    for r in rows:
        secs.setdefault(math.floor(float(r["measure_offset_s"])), []).append(r["outcome"] in OK)
    xs = sorted(secs)
    ax.plot(xs, [sum(secs[x]) / len(secs[x]) for x in xs], color=C1, linewidth=1.8)
    run = next(r for r in exp.manifest["runs"] if r["run_id"] == run_id)
    for f in run.get("faults", []):
        if f.get("ok"):
            a = f["actual_measure_offset_s"]
            ax.axvspan(a, a + f["spec"]["duration_s"], color=BAD, alpha=0.08, linewidth=0)
    ax.set_ylim(-0.05, 1.05)
    ax.set_title(title, fontsize=9.5)


def failure_view(exp: Exp, out: Path, prefix: str, title: str) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    res: dict[str, Any] = {"conditions": {}}
    conds = exp.conditions
    fig, axes = plt.subplots(1, len(conds), figsize=(3.3 * len(conds), 2.8), sharey=True)
    axes = axes if len(conds) > 1 else [axes]
    for ax, c in zip(axes, conds, strict=True):
        runs = exp.runs_of(c)
        timeline(ax, exp, runs[0]["run_id"], c)
        ax.set_xlabel("scheduled send time (s)")
        lat_all = [float(r["latency_ms"]) for m in runs for r in exp.measure(m["run_id"])
                   if r.get("http_status") is not None]
        fails = [float(r["latency_ms"]) for m in runs for r in exp.measure(m["run_id"])
                 if r["outcome"] in ("unavailable", "error")]
        attempts_inv = [inventory_attempts(exp, m["run_id"])[0] for m in runs]
        res["conditions"][c] = {
            "success_rate": stat_block(exp.run_level(c, "success_rate"), seed),
            "availability": stat_block(exp.run_level(c, "availability"), seed),
            "median_latency_ok": stat_block(exp.run_level(c, "latency_ok_ms.p50"), seed),
            "p95_latency_responded_per_run": [m["latency_responded_ms"]["p95"] for m in runs],
            "p99_latency_responded_per_run": [m["latency_responded_ms"]["p99"] for m in runs],
            "max_latency_responded_pooled_ms": round(max(lat_all), 1) if lat_all else None,
            "failed_latency_p50_pooled_ms": round(percentile(fails, 50), 1) if fails else None,
            "inventory_attempts_per_run": attempts_inv,
            "retries_per_run": exp.run_level(c, "retries"),
            "fallback_requests_per_run": exp.run_level(c, "fallback_requests"),
            "breaker_rejections_per_run": exp.run_level(c, "breaker_rejections"),
            "recovery_s": [r.get("recovery_s") for m in runs for r in (m.get("recovery") or [])],
            "settings": exp.manifest["scenario"]["conditions"][conds.index(c)]["resilience"],
        }
    axes[0].set_ylabel("success rate per second")
    fig.suptitle(title, fontsize=11, y=1.06)
    fig.text(0.01, -0.1, f"{exp.id} · shaded: fault window at its actual injection time · first run of each condition",
             fontsize=7.5, color=MUTED)
    res["figure_timeline"] = save(fig, out, f"{prefix}_timeline")
    return res


def analyse_D2(exp: Exp, out: Path) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    res: dict[str, Any] = {"retries": []}
    fig, ax = plt.subplots(figsize=(6, 3.6))
    for i, c in enumerate(exp.conditions):
        r = int(c.split("-")[1])
        vals = exp.run_level(c, "success_rate")
        pred = 1 - 0.5 ** (1 + r)
        ax.scatter([i] * len(vals), vals, s=22, color=C1, edgecolor="white", zorder=3)
        if vals:
            ax.plot([i - 0.15, i + 0.15], [median(vals)] * 2, color=C1, linewidth=3)
        ax.plot([i - 0.25, i + 0.25], [pred] * 2, color=MUTED, linestyle="--", linewidth=1.2)
        att = []
        for m in exp.runs_of(c):
            total, n = inventory_attempts(exp, m["run_id"])
            att.append(round(total / n, 4) if n else None)
        res["retries"].append({"retries": r, "predicted_success_if_independent": pred,
                               "success_rate": stat_block(vals, seed),
                               "inventory_attempts_per_request_per_run": att,
                               "median_latency_ok": stat_block(exp.run_level(c, "latency_ok_ms.p50"), seed),
                               "p95_latency_ok_per_run": exp.run_level(c, "latency_ok_ms.p95")})
    ax.set_xticks(range(len(exp.conditions)), exp.conditions)
    ax.set_ylabel("success rate (fraction of scheduled requests)")
    ax.set_title("D2 · Retries under 50% transient errors; dashed = 1 − 0.5^(1+r)")
    caption(ax, exp)
    res["figure"] = save(fig, out, "D_retries_transient")
    return res


def analyse_E(exp: Exp, out: Path) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    res: dict[str, Any] = {"levels": []}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.5, 3.6))
    conds = exp.conditions
    for i, c in enumerate(conds):
        offered = exp.run_level(c, "offered_rps")
        thr = exp.run_level(c, "throughput_ok_rps")
        p50 = exp.run_level(c, "latency_ok_ms.p50")
        p95 = exp.run_level(c, "latency_ok_ms.p95")
        lag = exp.run_level(c, "send_lag_ms.p95")
        a1.scatter([median(offered)] * len(thr), thr, color=C1, s=20, edgecolor="white", zorder=3)
        a2.scatter([median(offered)] * len(p50), p50, color=C1, s=20, edgecolor="white", zorder=3,
                   label="p50" if i == 0 else None)
        a2.scatter([median(offered)] * len(p95), p95, color=C2, s=20, edgecolor="white", zorder=3,
                   label="p95" if i == 0 else None)
        res["levels"].append({"condition": c, "offered_rps": stat_block(offered, seed),
                              "throughput_ok_rps": stat_block(thr, seed), "p50_ms": stat_block(p50, seed),
                              "p95_ms": stat_block(p95, seed), "send_lag_p95_ms": stat_block(lag, seed),
                              "success_rate": stat_block(exp.run_level(c, "success_rate"), seed),
                              "dropped_per_run": exp.run_level(c, "generator_dropped")})
    mx = max(lvl["offered_rps"]["median"] for lvl in res["levels"])
    a1.plot([0, mx], [0, mx], color=MUTED, linestyle="--", linewidth=1)
    a1.set_xlabel("offered load (req/s)")
    a1.set_ylabel("successful throughput (req/s)")
    a1.set_title("E · Throughput vs offered load (dashed: y = x)", fontsize=10)
    a2.set_xlabel("offered load (req/s)")
    a2.set_ylabel("latency of successful requests (ms)")
    a2.set_title("E · Latency percentiles per run", fontsize=10)
    a2.legend(frameon=False, fontsize=8)
    fig.text(0.01, -0.08, f"{exp.id} · local mode · p95 shown only for runs with ≥ 200 successes · "
             "this host and container limits only (0.5 CPU per service)",
             fontsize=7.5, color=MUTED)
    res["figure"] = save(fig, out, "E_scaling")
    stats_file = exp.dir / "docker_stats.csv"
    res["docker_stats"] = "present" if stats_file.exists() else "not recorded"
    if stats_file.exists():
        res["cpu_percent_median_per_run"] = cpu_per_run(exp, stats_file)
        res["cpu_note"] = ("docker stats sampled on the host (host clock, ~1-3 s apart), matched to each run's "
                           "start/finish time; 100% = one CPU; each service is limited to 0.5 CPU (50%)")
    return res


def cpu_per_run(exp: Exp, stats_file: Path) -> dict:
    import csv
    from datetime import datetime

    def t(x: str) -> datetime:
        return datetime.fromisoformat(x.replace("Z", "+00:00"))

    samples = list(csv.DictReader(stats_file.open(encoding="utf-8-sig")))
    out: dict[str, dict] = {}
    for run in exp.manifest["runs"]:
        a, b = t(run["started_at"]), t(run["finished_at"])
        per: dict[str, float | None] = {}
        for svc in ("gateway", "inventory-a", "pricing", "runner"):
            v = [float(x["cpu_percent"].rstrip("%")) for x in samples
                 if x["container"].endswith(f"-{svc}-1") and a <= t(x["host_time_utc"]) <= b]
            per[svc] = round(median(v), 1) if v else None
        out[run["run_id"]] = {"condition": run["condition"], **per}
    return out


def analyse_F(exp: Exp, out: Path) -> dict:
    seed = exp.manifest["scenario"]["seed"]
    base = exp.run_level("distributed", "latency_ok_ms.p50")
    res: dict[str, Any] = {"placements": {}}
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    dotplot(ax, exp, exp.conditions, "latency_ok_ms.p50")
    for c in exp.conditions:
        vals = exp.run_level(c, "latency_ok_ms.p50")
        res["placements"][c] = {"median_latency": stat_block(vals, seed),
                                "improvement_vs_distributed": diff_block(base, vals, seed)}
    ax.set_ylabel("median latency of successful requests (ms)")
    ax.set_title("F · Placement relative to the gateway (simulated mode, parallel calls)")
    caption(ax, exp)
    res["figure"] = save(fig, out, "F_placement")
    p = res["placements"]
    res["H4"] = {"pricing_local_improvement": p["pricing-local"]["improvement_vs_distributed"],
                 "inventory_local_improvement": p["inventory-local"]["improvement_vs_distributed"],
                 "criterion": "rejected if moving the non-critical dependency (inventory, 50 ms link) improves the "
                              "median comparably to moving the critical one (pricing, 80 ms link)"}
    return res


def analyse_G(exp: Exp, out: Path) -> dict:
    res = failure_view(exp, out, "G_hybrid_fallback", "G · Fallback to a replica in another simulated environment")
    seed = exp.manifest["scenario"]["seed"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.4))
    dotplot(a1, exp, exp.conditions, "success_rate")
    a1.set_ylabel("success rate")
    a1.set_title("G · Success rate", fontsize=10)
    dotplot(a2, exp, exp.conditions, "latency_ok_ms.p95")
    a2.set_ylabel("p95 latency of successful requests (ms)")
    a2.set_title("G · p95 latency of successful requests", fontsize=10)
    caption(a1, exp)
    res["figure_summary"] = save(fig, out, "G_hybrid_fallback")
    nf = exp.run_level("no-fallback", "success_rate")
    fb = exp.run_level("fallback", "success_rate")
    res["H3"] = {"success_gain_fallback_minus_none": diff_block(fb, nf, seed),
                 "p95_cost_fallback_minus_none": diff_block(exp.run_level("fallback", "latency_ok_ms.p95"),
                                                            exp.run_level("no-fallback", "latency_ok_ms.p95"), seed),
                 "criterion": "supported if fallback raises success and costs latency "
                              "(expected ≈ timeout + longer link)"}
    return res


ANALYSERS = {
    "A_baseline": analyse_A,
    "B_network_delay": analyse_B,
    "C_service_failure": lambda e, o: failure_view(e, o, "C_service_failure",
                                                   "C · Primary inventory failure, fallback off (local mode)"),
    "D_policies_hang": lambda e, o: failure_view(e, o, "D_policies_hang",
                                                 "D1 · Policies against an unresponsive dependency (local mode)"),
    "D_retries_transient": analyse_D2,
    "E_scaling": analyse_E,
    "F_placement": analyse_F,
    "G_hybrid_fallback": analyse_G,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "analysis")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    index = []
    for d in args.dirs:
        exp = Exp(d)
        entry = {"experiment_id": exp.id, "scenario": exp.scenario, "status": exp.summary.get("status"),
                 "validation_ok": exp.validation["ok"], "git_commit": exp.manifest.get("git_commit"),
                 "mode": exp.manifest.get("environment_mode"), "runs_completed": exp.summary.get("runs_completed"),
                 "overrides": exp.manifest.get("overrides") or {}, "warnings": exp.summary.get("warnings", [])}
        if not exp.ok():
            entry["skipped"] = "records did not validate or experiment did not complete"
            print(f"SKIP {d.name}: {entry['skipped']}: {exp.validation['problems']}")
            index.append(entry)
            continue
        fn = ANALYSERS.get(exp.scenario)
        if fn is None:
            print(f"no analyser for scenario {exp.scenario}; skipped")
            continue
        result = {"experiment": entry, **fn(exp, args.out)}
        (args.out / f"{exp.scenario}.json").write_text(json.dumps(result, indent=2, default=str) + "\n",
                                                       encoding="utf-8")
        index.append(entry)
        print(f"OK   {exp.scenario}: {exp.id}")
    (args.out / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
