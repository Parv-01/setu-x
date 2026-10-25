"""Experiment runner service (`runner` container; internal to the Compose network, not published).

It runs the load generator in the same clock domain as the services. The controller proxies its API
to the host (127.0.0.1:8090/api/experiments...).

GET  /api/scenarios                         scenario files in configs/scenarios (validated)
POST /api/experiments {"scenario": "A_baseline", "git_commit": "...", "label": "...",
                       "overrides": {"rate_rps", "warmup_s", "duration_s", "runs", "links"}}  (all optional, bounded)
                                            start one experiment (409 if one is already running)
GET  /api/experiments                       experiments in the results volume (newest first)
GET  /api/experiments/{id}                  status + progress (+ summary when finished)
GET  /api/experiments/{id}/live             provisional per-second counts of the run in progress
POST /api/experiments/{id}/stop             stop the running experiment (completed runs are kept)
GET  /api/experiments/{id}/files/{name}     manifest.json | observations.jsonl | observations.csv |
                                            events.jsonl | summary.json

Only scenario files from configs/scenarios can be run (by name), so the API cannot be used to send
arbitrary load or faults: every scenario is bounded by the Scenario schema (<= 100 req/s, <= 300 s
per run, <= 2 h total) and fault targets are the allow-listed downstream instances.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from setu_x.common.app import create_app, error_body
from setu_x.common.config import ConfigError, RunnerSettings
from setu_x.experiments.records import FILES
from setu_x.experiments.runner import ControlPlane, ExperimentError, ExperimentRunner
from setu_x.experiments.scenario import load_scenario, parse_scenario
from setu_x.placement import EnvironmentsConfig, PlacementConfig, load_config_dir

log = logging.getLogger("setu_x.runner")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,96}$")
MEDIA = {".json": "application/json", ".jsonl": "application/x-ndjson", ".csv": "text/csv"}


class Overrides(BaseModel):
    """Bounded changes to a scenario file (dashboard). They are applied to the file's content and the
    result is validated by the same Scenario schema, so the scenario bounds still hold."""

    model_config = ConfigDict(extra="forbid")
    rate_rps: float | None = Field(default=None, gt=0, le=100)
    warmup_s: float | None = Field(default=None, ge=0, le=60)
    duration_s: float | None = Field(default=None, gt=0, le=300)
    runs: int | None = Field(default=None, ge=1, le=20)
    links: dict[str, int] = Field(default_factory=dict, max_length=20,
                                  description="link delays (ms) merged into the scenario defaults; a condition "
                                              "that sets the same link keeps its own value")

    def apply(self, raw: dict) -> dict:
        out = copy.deepcopy(raw)
        w = out.setdefault("workload", {})
        for key in ("rate_rps", "warmup_s", "duration_s"):
            if getattr(self, key) is not None:
                w[key] = getattr(self, key)
        if self.runs is not None:
            out["runs"] = self.runs
        if self.links:
            d = out.setdefault("defaults", {})
            d["links"] = {**(d.get("links") or {}), **self.links}
        return out

    def given(self) -> dict:
        return self.model_dump(exclude_defaults=True)


class StartRequest(BaseModel):
    scenario: str = Field(pattern=NAME_RE.pattern, description="scenario file name without .yaml")
    overrides: Overrides | None = None
    git_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}(-dirty)?$")
    label: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9 _.,:()/-]*$")


def _leading_comment(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("#").strip())
    return "\n".join(lines).strip()


def _condition_view(sc, c) -> dict[str, Any]:
    return {"name": c.name, "description": c.description, "rate_rps": sc.rate_for(c), "call_mode": c.call_mode,
            "resilience": c.resilience, "instance_env": c.instance_env, "links": c.links,
            "faults": [{"target": f.target, "mode": f.mode, "at_s": f.at_s, "duration_s": f.duration_s,
                        "probability": f.probability} for f in c.faults]}


def build_app(settings: RunnerSettings, client: httpx.AsyncClient | None = None,
              load_client: httpx.AsyncClient | None = None,
              config: tuple[EnvironmentsConfig, PlacementConfig] | None = None) -> FastAPI:
    envs, base = config if config is not None else load_config_dir(settings.config_dir)
    scenario_dir = Path(settings.config_dir) / "scenarios"
    results_dir = Path(settings.results_dir)
    state: dict[str, Any] = {"current": None, "task": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["http"] = client or httpx.AsyncClient(trust_env=False)
        # A separate pool for load so control calls never queue behind load requests.
        state["load"] = load_client or httpx.AsyncClient(
            trust_env=False, limits=httpx.Limits(max_connections=300, max_keepalive_connections=300))
        results_dir.mkdir(parents=True, exist_ok=True)
        yield
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if client is None:
            await state["http"].aclose()
        if load_client is None:
            await state["load"].aclose()

    def readiness() -> dict[str, str]:
        return {"config": "ok", "results_dir": "ok" if results_dir.is_dir() else "missing",
                "scenarios": "ok" if scenario_dir.is_dir() else "missing"}

    app = create_app(settings, title="SETU-X experiment runner", readiness=readiness, lifespan=lifespan)
    app.state.runner_state = state

    def scenario_path(name: str) -> Path:
        return scenario_dir / f"{name}.yaml"

    @app.get("/api/scenarios", tags=["experiments"])
    async def scenarios() -> dict:
        out = []
        for p in sorted(scenario_dir.glob("*.yaml")):
            try:
                sc, _raw = load_scenario(p, envs, base)
                w = sc.workload
                out.append({"name": p.stem, "id": sc.id, "title": sc.title, "valid": True,
                            "requires_mode": sc.requires_mode, "hypotheses": sc.hypotheses,
                            "description": sc.description, "comment": _leading_comment(p),
                            "conditions": [c.name for c in sc.conditions], "runs": sc.runs,
                            "condition_detail": [_condition_view(sc, c) for c in sc.conditions],
                            "default_links": sc.defaults.links,
                            "rate_rps": w.rate_rps, "warmup_s": w.warmup_s, "duration_s": w.duration_s,
                            "arrival": w.arrival,
                            "estimated_s": round(sc.total_runs() * (w.warmup_s + w.duration_s + sc.cooldown_s
                                                                    + 1.5), 1)})
            except ConfigError as exc:
                out.append({"name": p.stem, "valid": False, "error": str(exc)})
        return {"scenarios": out}

    @app.post("/api/experiments", tags=["experiments"], status_code=202)
    async def start(body: StartRequest, request: Request):
        rid = request.state.request_id
        cur: ExperimentRunner | None = state["current"]
        if cur is not None and state["task"] is not None and not state["task"].done():
            return JSONResponse(status_code=409, content=error_body(
                "experiment_running", f"experiment {cur.id} is still running", rid, {"running": cur.describe()}))
        path = scenario_path(body.scenario)
        if not path.is_file():
            return JSONResponse(status_code=404, content=error_body(
                "unknown_scenario", f"no scenario '{body.scenario}' in configs/scenarios", rid))
        try:
            sc, raw = load_scenario(path, envs, base)
            overrides = body.overrides.given() if body.overrides else {}
            if overrides:
                sc = parse_scenario(body.overrides.apply(raw), f"{path.name} with overrides {overrides}", envs, base)
        except ConfigError as exc:
            return JSONResponse(status_code=422, content=error_body("invalid_scenario", str(exc), rid))
        control = ControlPlane(state["http"], settings.controller_url, settings.gateway_url)
        runner = ExperimentRunner(sc, path.read_text(encoding="utf-8"), f"configs/scenarios/{path.name}", envs, base,
                                  control, state["load"], results_dir, git_commit=body.git_commit,
                                  label=body.label, overrides=overrides)
        try:
            await runner.preflight()
        except ExperimentError as exc:
            return JSONResponse(status_code=409, content=error_body("preflight_failed", str(exc), rid))

        async def go() -> None:
            try:
                await runner.run()
            except ExperimentError as exc:  # preflight race: nothing written
                runner.status, runner.error = "failed", str(exc)
            log.info("experiment_finished", extra={"experiment_id": runner.id, "status": runner.status})

        state["current"] = runner
        state["task"] = asyncio.create_task(go())
        log.info("experiment_started", extra={"experiment_id": runner.id, "scenario": sc.id})
        return runner.describe()

    def from_disk(exp_id: str) -> dict | None:
        mpath = results_dir / exp_id / "manifest.json"
        if not mpath.is_file():
            return None
        m = json.loads(mpath.read_text(encoding="utf-8"))
        return {"experiment_id": m.get("experiment_id"), "scenario": m.get("scenario_id"), "title": m.get("title"),
                "status": m.get("status"), "error": m.get("error"), "warnings": m.get("warnings", []),
                "started_at": m.get("started_at"), "finished_at": m.get("finished_at"), "label": m.get("label"),
                "files": [f for f in FILES if (results_dir / exp_id / f).exists()]}

    @app.get("/api/experiments", tags=["experiments"])
    async def list_experiments() -> dict:
        items = []
        for d in sorted((p for p in results_dir.iterdir() if p.is_dir() and ID_RE.match(p.name)), reverse=True):
            cur = state["current"]
            if cur is not None and cur.id == d.name:
                items.append(cur.describe())
            else:
                info = from_disk(d.name)
                if info:
                    items.append(info)
        return {"experiments": items[:200]}

    @app.get("/api/experiments/{exp_id}", tags=["experiments"])
    async def get_experiment(exp_id: str, request: Request):
        cur = state["current"]
        if cur is not None and cur.id == exp_id:
            out = cur.describe()
        elif ID_RE.match(exp_id) and (info := from_disk(exp_id)) is not None:
            out = info
        else:
            return JSONResponse(status_code=404, content=error_body("not_found", "unknown experiment",
                                                                    request.state.request_id))
        spath = results_dir / exp_id / "summary.json"
        if out.get("status") in ("completed", "stopped", "failed") and spath.is_file():
            s = json.loads(spath.read_text(encoding="utf-8"))
            out["validation"] = s.get("validation")
            out["conditions"] = {k: {"runs": v["runs"], "run_level": {m: v["run_level"][m] for m in (
                "median_latency_ok_ms", "success_rate", "availability") if m in v["run_level"]}}
                for k, v in (s.get("conditions") or {}).items()}
        return out

    @app.get("/api/experiments/{exp_id}/live", tags=["experiments"])
    async def live(exp_id: str, request: Request):
        cur = state["current"]
        if cur is None or cur.id != exp_id:
            return JSONResponse(status_code=404, content=error_body(
                "not_live", "live data exists only for the experiment running (or last run) in this runner process",
                request.state.request_id))
        return {"experiment_id": cur.id, "status": cur.status, "progress": dict(cur.progress), **cur.live.snapshot()}

    @app.post("/api/experiments/{exp_id}/stop", tags=["experiments"])
    async def stop(exp_id: str, request: Request):
        cur, task = state["current"], state["task"]
        if cur is None or cur.id != exp_id or task is None or task.done():
            return JSONResponse(status_code=409, content=error_body(
                "not_running", "that experiment is not running", request.state.request_id))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return cur.describe()

    @app.get("/api/experiments/{exp_id}/files/{name}", tags=["experiments"])
    async def get_file(exp_id: str, name: str, request: Request):
        path = results_dir / exp_id / name
        if not ID_RE.match(exp_id) or name not in FILES or not path.is_file():
            return JSONResponse(status_code=404, content=error_body("not_found", "unknown experiment or file",
                                                                    request.state.request_id))
        return FileResponse(path, media_type=MEDIA[path.suffix], filename=name)

    return app
