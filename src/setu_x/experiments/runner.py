"""Experiment orchestration: isolation per run, load, faults, records, summary.

For every run (one condition x one repetition), in the scenario's run order:

  1. reset   controller: placement + link matrix back to the files; clear every fault
  2. apply   controller: PUT the condition's placement and/or link changes (validated there too)
  3. sync    gateway: POST /api/resilience/reset {seed} -> fetches the placement now, resets
             breakers and routing state, reseeds jitter. The returned placement version and sha256
             must equal the controller's, otherwise the run is not started.
  4. load    open-loop schedule (warm-up + measurement); scheduled faults are injected through the
             controller at `at_s` seconds after the measurement phase starts
  5. collect wait for in-flight requests, clear faults, fetch the run's gateway and controller
             events (seq > value captured before the run), append rows/events to disk
  6. cool down

After the last run the observation files are re-read from disk, validated, and summarised.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from setu_x import __version__
from setu_x.experiments.loadgen import generate, plan_requests
from setu_x.experiments.metrics import run_metrics
from setu_x.experiments.records import RecordWriter, validate_records, write_json
from setu_x.experiments.scenario import Scenario, links_for, placement_for
from setu_x.experiments.schedule import arrival_offsets, run_order, run_seed
from setu_x.experiments.statistics import compare, condition_summary
from setu_x.placement import EnvironmentsConfig, PlacementConfig

CLOCK_NOTE = ("latency measured by the runner process with time.perf_counter (monotonic), inside the Compose "
              "network, i.e. in the same clock domain as the services")


class ExperimentError(RuntimeError):
    """The experiment cannot start or a run cannot be set up correctly (nothing is measured)."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_experiment_id(scenario_id: str) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{scenario_id}-{secrets.token_hex(3)}"


def _package_versions() -> dict[str, str]:
    out = {}
    for name in ("fastapi", "httpx", "pydantic", "uvicorn", "PyYAML"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def _cgroup_cpu_limit() -> str:
    try:  # cgroup v2
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        return "unlimited" if quota == "max" else f"{int(quota) / int(period):.2f} CPU"
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        return "unlimited" if quota < 0 else f"{quota / period:.2f} CPU"
    except (OSError, ValueError):
        return "unknown"


class ControlPlane:
    """HTTP calls to the controller and gateway used for run isolation."""

    def __init__(self, http: httpx.AsyncClient, controller_url: str, gateway_url: str) -> None:
        self.http = http
        self.controller = controller_url.rstrip("/")
        self.gateway = gateway_url.rstrip("/")

    async def _json(self, method: str, url: str, **kw: Any) -> Any:
        try:
            r = await self.http.request(method, url, timeout=kw.pop("timeout", 5.0), **kw)
        except httpx.HTTPError as exc:
            raise ExperimentError(f"{method} {url}: {type(exc).__name__}") from None
        if r.status_code >= 400:
            raise ExperimentError(f"{method} {url}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    async def stack_info(self) -> dict[str, Any]:
        gw = await self._json("GET", self.gateway + "/health/ready")
        ctl = await self._json("GET", self.controller + "/health/ready")
        envs = await self._json("GET", self.controller + "/api/environments")
        return {"gateway": gw, "controller": ctl, "controller_mode": envs.get("mode")}

    async def reset(self) -> None:
        await self._json("POST", self.controller + "/api/placement/reset")
        await self.clear_faults()

    async def clear_faults(self) -> None:
        res = await self._json("DELETE", self.controller + "/api/faults")
        failed = [r["target"] for r in res.get("results", []) if not r.get("cleared")]
        if failed:
            raise ExperimentError(f"could not clear faults on {failed}")

    async def apply(self, placement: PlacementConfig | None, links: dict[str, int] | None) -> dict[str, Any]:
        if placement is not None:
            await self._json("PUT", self.controller + "/api/placement", json=placement.model_dump())
        if links:
            await self._json("PUT", self.controller + "/api/links", json={"link_rtt_ms": links})
        return await self._json("GET", self.controller + "/api/placement")

    async def sync_gateway(self, seed: int, expect_version: int, expect_sha: str, attempts: int = 10) -> dict:
        last: dict[str, Any] = {}
        for _ in range(attempts):
            last = await self._json("POST", self.gateway + "/api/resilience/reset", json={"seed": seed, "sync": True})
            if last.get("placement_version") == expect_version and last.get("placement_sha256") == expect_sha:
                return last
            await asyncio.sleep(0.2)
        raise ExperimentError(f"gateway did not apply placement version {expect_version} (has "
                              f"{last.get('placement_version')}, source {last.get('placement_source')})")

    async def inject(self, target: str, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", self.controller + "/api/faults", json={"target": target, **spec})

    async def last_seqs(self) -> tuple[int, int]:
        g = await self._json("GET", self.gateway + "/api/resilience/events", params={"limit": 1})
        c = await self._json("GET", self.controller + "/api/events", params={"limit": 1})
        return int(g.get("last_seq", 0)), int(c.get("last_seq", 0))

    async def events_since(self, gw_seq: int, ctl_seq: int) -> tuple[list[dict], list[dict], list[str]]:
        warnings = []
        g = await self._json("GET", self.gateway + "/api/resilience/events",
                             params={"limit": 20000, "since": gw_seq}, timeout=10.0)
        c = await self._json("GET", self.controller + "/api/events", params={"limit": 1000, "since": ctl_seq})
        for name, body, seq in (("gateway", g, gw_seq), ("controller", c, ctl_seq)):
            oldest = body.get("oldest_seq")
            if oldest is not None and oldest > seq + 1:
                warnings.append(f"{name} event log wrapped during the run: events {seq + 1}..{oldest - 1} lost")
        return list(reversed(g.get("events", []))), list(reversed(c.get("events", []))), warnings


class ExperimentRunner:
    def __init__(
        self,
        scenario: Scenario,
        scenario_text: str,
        scenario_file: str,
        envs: EnvironmentsConfig,
        base_placement: PlacementConfig,
        control: ControlPlane,
        load_client: httpx.AsyncClient,
        results_dir: Path,
        *,
        experiment_id: str | None = None,
        git_commit: str | None = None,
        label: str = "",
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.scenario = scenario
        self.scenario_text = scenario_text
        self.scenario_file = scenario_file
        self.envs = envs
        self.base = base_placement
        self.control = control
        self.load_client = load_client
        self.id = experiment_id or new_experiment_id(scenario.id)
        self.dir = results_dir / self.id
        self.git_commit = git_commit or "unknown"
        self.label = label
        self.clock = clock
        self.status = "created"
        self.error: str | None = None
        self.warnings: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.progress: dict[str, Any] = {"run": 0, "total_runs": scenario.total_runs(), "condition": None,
                                         "phase": None, "completed_requests": 0, "scheduled_requests": 0}
        self.manifest: dict[str, Any] = {}
        self.summary: dict[str, Any] | None = None

    # -------------------------------------------------------------------------------- helpers
    def describe(self) -> dict[str, Any]:
        return {"experiment_id": self.id, "scenario": self.scenario.id, "title": self.scenario.title,
                "status": self.status, "error": self.error, "progress": dict(self.progress),
                "warnings": list(self.warnings), "started_at": self.manifest.get("started_at"),
                "finished_at": self.manifest.get("finished_at"), "label": self.label,
                "files": [f for f in ("manifest.json", "observations.jsonl", "observations.csv", "events.jsonl",
                                      "summary.json") if (self.dir / f).exists()]}

    def _write_manifest(self) -> None:
        self.manifest.update(status=self.status, error=self.error, warnings=self.warnings, runs=self.runs)
        write_json(self.dir / "manifest.json", self.manifest)

    def _payload(self, seq: int) -> dict[str, Any]:
        w = self.scenario.workload
        return {"sku": w.skus[seq % len(w.skus)], "quantity": w.quantity, "customer_tier": w.customer_tier}

    # -------------------------------------------------------------------------------- main
    async def preflight(self) -> dict[str, Any]:
        info = await self.control.stack_info()
        gw_mode = info["gateway"].get("mode")
        if gw_mode != self.scenario.requires_mode or info["controller_mode"] != self.scenario.requires_mode:
            raise ExperimentError(
                f"scenario '{self.scenario.id}' requires SETU_MODE={self.scenario.requires_mode}, but the stack runs "
                f"gateway={gw_mode}, controller={info['controller_mode']}. Restart it with "
                f"SETU_MODE={self.scenario.requires_mode} (docker compose up -d) and try again.")
        if info["gateway"].get("status") != "ready":
            raise ExperimentError("gateway is not ready")
        return info

    async def run(self) -> dict[str, Any]:
        s = self.scenario
        info = await self.preflight()
        self.status = "running"
        writer = RecordWriter(self.dir)
        order = run_order([c.name for c in s.conditions], s.runs, s.order, s.seed)
        index = {c.name: i for i, c in enumerate(s.conditions)}
        self.manifest = {
            "experiment_id": self.id, "label": self.label, "title": s.title, "hypotheses": s.hypotheses,
            "scenario_id": s.id, "scenario_file": self.scenario_file,
            "scenario_sha256": hashlib.sha256(self.scenario_text.encode("utf-8")).hexdigest(),
            "scenario_text": self.scenario_text, "scenario": s.model_dump(),
            "git_commit": self.git_commit, "setu_x_version": __version__,
            "python": sys.version.split()[0], "packages": _package_versions(),
            "platform": platform.platform(), "host_cpu_count_visible": os.cpu_count(),
            "runner_cpu_limit": _cgroup_cpu_limit(), "environment_mode": s.requires_mode,
            "stack": {"gateway": info["gateway"], "controller": info["controller"]},
            "base_placement": self.base.model_dump(), "base_link_rtt_ms": dict(self.envs.link_rtt_ms),
            "run_order": [{"condition": c, "run_index": r, "seed": run_seed(s.seed, index[c], r)} for c, r in order],
            "clock": CLOCK_NOTE,
            "resource_usage": "not measured by the runner (use scripts/run_experiment.ps1 -DockerStats on the host)",
            "started_at": utc_now(), "finished_at": None,
        }
        self._write_manifest()
        outcome = "completed"
        try:
            for n, (cname, r) in enumerate(order, start=1):
                cond = s.conditions[index[cname]]
                self.progress.update(run=n, condition=cname, run_index=r, phase="setup", completed_requests=0)
                await self._one_run(writer, cond, index[cname], r)
                self._write_manifest()
                if n < len(order) and s.cooldown_s:
                    self.progress["phase"] = "cooldown"
                    await asyncio.sleep(s.cooldown_s)
        except asyncio.CancelledError:
            outcome = "stopped"
            self.warnings.append("stopped before completion: only completed runs are recorded and summarised")
        except ExperimentError as exc:
            outcome, self.error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded in the manifest, never hidden
            outcome, self.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            # "analysing" until summary.json is written, so a finished status always has a summary
            self.status = "analysing"
            self.progress["phase"] = "analysing"
            writer.close()
            try:
                await asyncio.shield(self.control.reset())
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"final reset failed: {exc}")
            self.summary = self.analyse(outcome)
            self.manifest["finished_at"] = utc_now()
            self.status = outcome
            self._write_manifest()
            self.progress["phase"] = "done"
        return self.summary

    async def _one_run(self, writer: RecordWriter, cond, ci: int, r: int) -> None:
        s, w = self.scenario, self.scenario.workload
        seed = run_seed(s.seed, ci, r)
        run_id = f"{cond.name}-r{r}"
        await self.control.reset()
        placement = placement_for(cond, self.base, self.envs)
        changed_placement = placement.fingerprint() != self.base.fingerprint()
        links = links_for(cond, self.envs).link_rtt_ms if cond.links else None
        snap = await self.control.apply(placement if changed_placement else None, links)
        if snap["sha256"] != placement.fingerprint():
            raise ExperimentError(f"run {run_id}: controller placement sha256 does not match the condition")
        synced = await self.control.sync_gateway(seed, snap["version"], snap["sha256"])
        gw_seq, ctl_seq = await self.control.last_seqs()

        offsets = arrival_offsets(s.rate_for(cond), w.warmup_s + w.duration_s, w.arrival, seed)
        prefix = f"{s.id}-{self.id[-6:]}-c{ci}-r{r}"
        plan = plan_requests(offsets, w.warmup_s, prefix, self._payload)
        self.progress.update(phase="load", scheduled_requests=len(plan))
        start = self.clock() + 0.2
        base_row = {"experiment_id": self.id, "condition": cond.name, "run_index": r, "run_id": run_id}
        fault_log: list[dict[str, Any]] = []
        fault_tasks = [asyncio.create_task(self._fault_at(start + w.warmup_s + f.at_s, f, seed, fault_log, start))
                       for f in cond.faults]

        def progress(done: int) -> None:
            self.progress["completed_requests"] = done

        started_utc = utc_now()
        try:
            rows = await generate(self.load_client, self.control.gateway + "/api/orders", plan, start=start,
                                  max_in_flight=w.max_in_flight, timeout_s=w.request_timeout_s, base_row=base_row,
                                  clock=self.clock, on_progress=progress)
        finally:
            for t in fault_tasks:
                t.cancel()
            await asyncio.gather(*fault_tasks, return_exceptions=True)
        self.progress["phase"] = "collect"
        await self.control.clear_faults()
        gw_events, ctl_events, ev_warn = await self.control.events_since(gw_seq, ctl_seq)
        warnings = list(ev_warn)
        versions = {row.get("placement_version") for row in rows if row.get("placement_version") is not None}
        if versions - {snap["version"]}:
            warnings.append(f"run {run_id}: responses carried placement versions {sorted(versions)}, "
                            f"expected {snap['version']}")
        late = [f for f in fault_log if f.get("lateness_ms", 0) > 100]
        if late:
            warnings.append(f"run {run_id}: {len(late)} fault(s) injected >100 ms late")
        failed_faults = [f for f in fault_log if not f.get("ok")]
        if failed_faults:
            warnings.append(f"run {run_id}: {len(failed_faults)} fault injection(s) failed")
        writer.write_rows(rows)
        writer.write_events([{"run_id": run_id, "source": "gateway", **e} for e in gw_events]
                            + [{"run_id": run_id, "source": "controller", **e} for e in ctl_events]
                            + [{"run_id": run_id, "source": "runner", "type": "fault_scheduled", **f}
                               for f in fault_log])
        self.warnings.extend(warnings)
        self.runs.append({
            "run_id": run_id, "condition": cond.name, "condition_index": ci, "run_index": r, "seed": seed,
            "started_at": started_utc, "finished_at": utc_now(), "scheduled": len(plan),
            "measure_scheduled": sum(1 for p in plan if p.phase == "measure"),
            "rate_rps": s.rate_for(cond),
            "placement_version": snap["version"], "placement_sha256": snap["sha256"],
            "link_rtt_ms": snap.get("link_rtt_ms"), "gateway_reset": {k: synced.get(k) for k in (
                "placement_version", "placement_sha256", "seed", "synced")},
            "faults": fault_log, "warnings": warnings,
        })

    async def _fault_at(self, when: float, fault, seed: int, log: list[dict[str, Any]], start: float) -> None:
        delay = when - self.clock()
        if delay > 0:
            await asyncio.sleep(delay)
        spec = fault.model_dump(exclude={"target", "at_s"})
        if spec.get("seed") is None and spec.get("probability", 1.0) < 1.0:
            spec["seed"] = seed  # reproducible probabilistic faults
        entry = {"target": fault.target, "at_s": fault.at_s, "spec": spec,
                 "actual_measure_offset_s": round(self.clock() - start - self.scenario.workload.warmup_s, 4),
                 "lateness_ms": round(max(0.0, (self.clock() - when) * 1000), 3)}
        try:
            await self.control.inject(fault.target, spec)
            entry["ok"] = True
        except ExperimentError as exc:
            entry.update(ok=False, error=str(exc))
        log.append(entry)

    # -------------------------------------------------------------------------------- analysis
    def analyse(self, status: str) -> dict[str, Any]:
        return analyse_records(self.dir, self.scenario, self.runs, {
            "experiment_id": self.id, "status": status, "git_commit": self.git_commit,
            "warnings": self.warnings})


def analyse_records(directory: Path, s: Scenario, runs: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Validate the records in `directory` (re-read from disk) and write summary.json."""
    try:
        valid, validation = validate_records(directory, runs)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        summary = {**meta, "validation": {"ok": False, "problems": [f"cannot read records: {exc}"]}}
        write_json(directory / "summary.json", summary)
        return summary
    by_run: dict[str, list[dict]] = {}
    for row in valid:
        by_run.setdefault(row["run_id"], []).append(row)
    faults_by_cond = {c.name: [f.model_dump() for f in c.faults] for c in s.conditions}
    recovery = {"window_s": s.recovery_window_s, "hold_s": s.recovery_hold_s, "threshold": s.recovery_threshold}
    per_run = []
    for run in runs:
        # recovery uses the ACTUAL injection time (fault expiry is relative to it), when recorded
        actual = [{"at_s": f["actual_measure_offset_s"], "duration_s": f["spec"]["duration_s"]}
                  for f in run.get("faults", []) if f.get("ok") and "actual_measure_offset_s" in f]
        faults = actual if actual else faults_by_cond.get(run["condition"])
        m = run_metrics(by_run.get(run["run_id"], []), s.workload.duration_s, faults, recovery)
        per_run.append({"run_id": run["run_id"], "condition": run["condition"], "run_index": run["run_index"],
                        "seed": run["seed"], **m})
    conditions = {}
    for c in s.conditions:
        runs_c = [m for m in per_run if m["condition"] == c.name]
        if not runs_c:
            continue
        pooled = [float(r["latency_ms"]) for r in valid
                  if r["condition"] == c.name and r["phase"] == "measure" and r["outcome"] in ("confirmed", "rejected")]
        conditions[c.name] = condition_summary(runs_c, pooled, seed=s.seed)
    reference = s.conditions[0].name
    comparisons = compare(conditions, reference, seed=s.seed) if reference in conditions and len(conditions) > 1 else []
    summary = {
        "experiment_id": meta.get("experiment_id"), "scenario_id": s.id, "status": meta.get("status"),
        "environment_mode": s.requires_mode, "git_commit": meta.get("git_commit"),
        "measurement_window_s": s.workload.duration_s, "warmup_s": s.workload.warmup_s,
        "runs_completed": len(runs), "runs_planned": s.total_runs(),
        "validation": validation, "per_run": per_run, "conditions": conditions,
        "comparisons": {"reference": reference, "method": "difference of medians of run-level statistics; "
                        "95% percentile-bootstrap CI over runs (B=2000, seeded)", "results": comparisons},
        "notes": [
            "latency = scheduled send time -> response, measured inside the Compose network (open-loop)",
            "percentiles are type-7 linear interpolation; p90/p95/p99 need n >= 100/200/1000 per run",
            "per-run percentiles are never averaged; pooled values are labelled POOLED",
            "success = confirmed or rejected; availability additionally counts degraded",
            "recovery_s is measured from fault end (actual injection time + duration) with the rolling window "
            "and hold time given per fault",
            "simulated environments are labels on one Docker host, not cloud deployments",
        ],
        "warnings": meta.get("warnings", []),
    }
    write_json(directory / "summary.json", summary)
    return summary
