"""Controller service (local-development control plane; published on 127.0.0.1 only, no auth).

GET  /api/environments     environments, simulated flags, RTT matrix, mode, per-env adapter info
GET  /api/adapters         every adapter: integration status, per-operation support, reasons
GET  /api/placement        current placement {version, sha256, source, updated_at, placement}
PUT  /api/placement        replace placement after validation (422 envelope on any problem)
POST /api/placement/reset  restore placement.yaml AND the link matrix from environments.yaml
PUT  /api/links            change inter-environment link delays at runtime (partial update, validated)
GET  /api/services         health of the gateway and every placed instance (via the local adapter)
POST /api/faults           inject a bounded fault into an allow-listed instance (proxied to /admin/fault)
GET  /api/faults           active fault on every allow-listed instance
DELETE /api/faults/{name}  clear one; DELETE /api/faults clears all
GET  /api/events           controller event log (placement/link changes, faults), newest first
*    /api/experiments...   proxied to the internal experiment runner (Phase 5): GET/POST
     /api/experiments, GET /api/experiments/{id}, GET /api/experiments/{id}/live,
     POST /api/experiments/{id}/stop, GET /api/experiments/{id}/files/{name}, GET /api/scenarios
GET  /api/gateway/...      read-only gateway views for the dashboard (routing decisions, resilience
                           events and breakers, current placement, dependency health)
POST /api/gateway/orders   one test order through the gateway (dashboard "send test order")
GET  /                     redirects to the dashboard (/dashboard/, Phase 6)

Browser-facing protections (Phase 6; the controller has no authentication and is a local-development
control surface published on 127.0.0.1 only):
  * Host header must be one of SETU_ALLOWED_HOSTS (defence against DNS rebinding).
  * State-changing requests (POST/PUT/DELETE/PATCH) that carry an Origin header are refused unless the
    origin is the controller itself (same origin) or http://127.0.0.1, http://localhost or http://[::1]
    (any port), so another website open in the same browser cannot drive the control plane.
  * The dashboard is served with a strict Content-Security-Policy (same-origin scripts only).

Safety rule for PUT: instance names and URLs are fixed by configs/placement.yaml. The API can
change env labels, roles, weights, enabled flags, routing policy, call mode and resilience
settings, but cannot point the gateway at a new URL.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask

from setu_x.adapters import build_adapters
from setu_x.adapters.base import ServiceEndpoint
from setu_x.common.app import create_app, error_body
from setu_x.common.config import ConfigError, ControllerSettings
from setu_x.faults import FaultSpec
from setu_x.placement import EnvironmentsConfig, PlacementConfig, load_config_dir, parse_environments, parse_placement
from setu_x.resilience import EventLog

log = logging.getLogger("setu_x.controller")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class PlacementStore:
    def __init__(self, envs: EnvironmentsConfig, file_placement: PlacementConfig) -> None:
        self.envs = envs
        self.file_placement = file_placement
        self.current = file_placement
        self.file_links = dict(envs.link_rtt_ms)
        self.links = dict(envs.link_rtt_ms)
        self.links_source = "file"
        self.version = 1
        self.source = "file"
        self.updated_at = _now()

    def snapshot(self) -> dict[str, Any]:
        return {"version": self.version, "sha256": self.current.fingerprint(), "source": self.source,
                "updated_at": self.updated_at, "placement": self.current.model_dump(),
                "link_rtt_ms": dict(self.links), "links_source": self.links_source,
                "link_rtt_note": "configured (injected) delays, not measured provider latencies; "
                                 "applied only when the gateway runs with SETU_MODE=simulated"}

    def update_links(self, changes: Any) -> None:
        if not isinstance(changes, dict) or not changes:
            raise ConfigError("request body: expected {\"link_rtt_ms\": {\"<env-a>--<env-b>\": <ms>, ...}}")
        merged = dict(self.links)
        canonical = {frozenset(k.split("--")): k for k in merged}
        for key, value in changes.items():
            existing = canonical.get(frozenset(str(key).split("--")))
            merged[existing or key] = value
        # validate the complete matrix with the same rules as environments.yaml
        parse_environments({"environments": self.envs.model_dump()["environments"], "link_rtt_ms": merged},
                           "request body link_rtt_ms")
        self.links, self.links_source = merged, "api"
        self.version += 1
        self.updated_at = _now()

    def replace(self, data: Any) -> PlacementConfig:
        candidate = parse_placement(data, self.envs, "request body")
        if candidate.identity() != self.file_placement.identity():
            allowed = sorted(f"{s}:{n}={u}" for s, n, u in self.file_placement.identity())
            raise ConfigError("request body: instance names and URLs must match configs/placement.yaml "
                              f"(allowed: {', '.join(allowed)})")
        self.current, self.source = candidate, "api"
        self.version += 1
        self.updated_at = _now()
        return candidate

    def reset(self) -> None:
        self.current, self.source = self.file_placement, "file"
        self.links, self.links_source = dict(self.file_links), "file"
        self.version += 1
        self.updated_at = _now()


def build_app(
    settings: ControllerSettings,
    client: httpx.AsyncClient | None = None,
    config: tuple[EnvironmentsConfig, PlacementConfig] | None = None,
    environ: dict[str, str] | None = None,
) -> FastAPI:
    envs, file_placement = config if config is not None else load_config_dir(settings.config_dir)
    store = PlacementStore(envs, file_placement)
    owns_client = client is None
    state: dict[str, Any] = {}
    events = EventLog(maxlen=1000)
    # Fault-injection targets: downstream instances from placement.yaml only (never gateway/controller).
    fault_targets = {name: url for _svc, name, url in file_placement.identity()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = client or httpx.AsyncClient(trust_env=False)
        adapters, adapter_errors = build_adapters(envs, store.current, http, environ)
        adapters["local-docker"].extra_endpoints = [
            ServiceEndpoint("gateway", "gateway", settings.env if settings.env in envs.environments else "local",
                            settings.gateway_url, False, "primary", True)
        ] + ([ServiceEndpoint("runner", "runner", "local", settings.runner_url, False, "primary", True)]
             if settings.runner_url else [])
        state.update(http=http, adapters=adapters, adapter_errors=adapter_errors)
        for name, err in adapter_errors.items():
            log.warning("adapter_config_invalid", extra={"adapter": name, "error": err})
        yield
        if owns_client:
            await http.aclose()

    def readiness() -> dict[str, str]:
        return {"config": "ok", "adapters": "ok" if "adapters" in state else "not_started"}

    app = create_app(settings, title="SETU-X controller", readiness=readiness, lifespan=lifespan)
    app.state.store = store
    app.state.events = events
    install_browser_protections(app, settings)

    def provider_adapter_info(provider: str) -> dict[str, Any] | None:
        if provider in state.get("adapter_errors", {}):
            return {"name": provider, "status": "config_error", "error": state["adapter_errors"][provider]}
        a = state.get("adapters", {}).get(provider)
        return {"name": a.name, "status": a.status().value, "status_detail": a.status_detail()} if a else None

    @app.get("/api/environments", tags=["environments"])
    async def environments() -> dict:
        out = []
        for name, e in envs.environments.items():
            out.append({
                "name": name,
                "provider": e.provider,
                "simulated": e.simulated,
                "description": e.description,
                "served_by_adapter": "local-docker",
                "label_notice": ("simulation label on the local Docker host; no cloud resources involved"
                                 if e.simulated else "the local Docker host"),
                "real_provider_adapter": provider_adapter_info(e.provider) if e.provider != "local" else None,
            })
        return {"mode": settings.mode, "environments": out, "link_rtt_ms": envs.link_rtt_ms,
                "link_rtt_note": "illustrative configured values, not measured provider latencies; "
                                 "injected only in simulated mode (Phase 4)"}

    @app.get("/api/adapters", tags=["adapters"])
    async def adapters() -> dict:
        return {"adapters": [a.describe() for a in state["adapters"].values()],
                "config_errors": state["adapter_errors"]}

    @app.get("/api/placement", tags=["placement"])
    async def get_placement() -> dict:
        return store.snapshot()

    @app.put("/api/placement", tags=["placement"])
    async def put_placement(request: Request):
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse(status_code=422, content=error_body(
                "invalid_placement", "request body must be JSON", request.state.request_id))
        try:
            store.replace(data)
        except ConfigError as exc:
            return JSONResponse(status_code=422, content=error_body(
                "invalid_placement", str(exc), request.state.request_id, {"current_version": store.version}))
        state["adapters"]["local-docker"].update(envs, store.current)
        log.info("placement_updated", extra={"version": store.version, "sha256": store.current.fingerprint()})
        events.add("placement_updated", version=store.version, sha256=store.current.fingerprint())
        return store.snapshot()

    @app.put("/api/links", tags=["placement"])
    async def put_links(request: Request):
        try:
            data = await request.json()
            store.update_links(data.get("link_rtt_ms") if isinstance(data, dict) else None)
        except ValueError as exc:  # JSON decode errors and ConfigError are both ValueError
            msg = str(exc) if isinstance(exc, ConfigError) else "request body must be JSON"
            return JSONResponse(status_code=422, content=error_body(
                "invalid_links", msg, request.state.request_id, {"current_version": store.version}))
        events.add("links_updated", version=store.version, link_rtt_ms=store.links)
        return store.snapshot()

    @app.post("/api/placement/reset", tags=["placement"])
    async def reset_placement() -> dict:
        store.reset()
        state["adapters"]["local-docker"].update(envs, store.current)
        log.info("placement_reset", extra={"version": store.version})
        events.add("placement_reset", version=store.version)
        return store.snapshot()

    # ------------------------------------------------------------------ fault injection proxy
    @app.post("/api/faults", tags=["faults"])
    async def inject_fault(request: Request):
        rid = request.state.request_id
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse(status_code=422, content=error_body("invalid_fault", "request body must be JSON", rid))
        if not isinstance(data, dict):
            return JSONResponse(status_code=422, content=error_body("invalid_fault", "expected a JSON object", rid))
        target = data.pop("target", None)
        if target not in fault_targets:
            return JSONResponse(status_code=422, content=error_body(
                "invalid_fault", f"target must be one of {sorted(fault_targets)} (got {target!r})", rid))
        try:
            spec = FaultSpec.model_validate(data)
        except ValidationError as exc:
            details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
            return JSONResponse(status_code=422,
                                content=error_body("invalid_fault", "invalid fault spec", rid, details))
        try:
            r = await state["http"].post(fault_targets[target] + "/admin/fault", json=spec.model_dump(), timeout=2.0)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            events.add("fault_inject_failed", target=target, error=type(exc).__name__)
            return JSONResponse(status_code=502, content=error_body(
                "target_unreachable", f"could not reach {target} ({type(exc).__name__})", rid))
        events.add("fault_injected", target=target, **spec.model_dump())
        return {"target": target, **r.json()}

    @app.get("/api/faults", tags=["faults"])
    async def list_faults() -> dict:
        async def one(name: str, url: str) -> dict:
            try:
                r = await state["http"].get(url + "/admin/fault", timeout=1.0)
                return {"target": name, "reachable": True, "active": r.json().get("active")}
            except (httpx.HTTPError, ValueError) as exc:
                return {"target": name, "reachable": False, "error": type(exc).__name__}

        return {"faults": list(await asyncio.gather(*(one(n, u) for n, u in sorted(fault_targets.items()))))}

    async def _clear(name: str) -> dict:
        try:
            r = await state["http"].delete(fault_targets[name] + "/admin/fault", timeout=2.0)
            r.raise_for_status()
            events.add("fault_cleared", target=name)
            return {"target": name, "cleared": True}
        except httpx.HTTPError as exc:
            return {"target": name, "cleared": False, "error": type(exc).__name__}

    @app.delete("/api/faults/{name}", tags=["faults"])
    async def clear_fault(name: str, request: Request):
        if name not in fault_targets:
            return JSONResponse(status_code=404, content=error_body(
                "unknown_target", f"target must be one of {sorted(fault_targets)}", request.state.request_id))
        return await _clear(name)

    @app.delete("/api/faults", tags=["faults"])
    async def clear_all_faults() -> dict:
        return {"results": list(await asyncio.gather(*(_clear(n) for n in sorted(fault_targets))))}

    @app.get("/api/events", tags=["events"])
    async def get_events(limit: int = Query(default=100, ge=1, le=1000),
                         since: int | None = Query(default=None, ge=0)) -> dict:
        return {"events": events.recent(limit, since=since), "last_seq": events.last_seq,
                "oldest_seq": events.oldest_seq}

    # ------------------------------------------------------------------ experiment runner proxy
    runner_url = settings.runner_url.rstrip("/")
    id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,96}$")
    file_re = re.compile(r"^[a-z_]+\.(json|jsonl|csv)$")

    async def to_runner(request: Request, method: str, path: str, stream: bool = False):
        rid = request.state.request_id
        if not runner_url:
            return JSONResponse(status_code=503, content=error_body(
                "runner_disabled", "SETU_RUNNER_URL is empty; the experiment runner is not configured", rid))
        body = await request.body() if method == "POST" else None
        req = state["http"].build_request(
            method, runner_url + path, content=body, params=dict(request.query_params),
            headers={"content-type": "application/json", "X-Request-ID": rid}, timeout=10.0)
        try:
            r = await state["http"].send(req, stream=stream)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=503, content=error_body(
                "runner_unreachable", f"experiment runner not reachable ({type(exc).__name__})", rid))
        if stream:
            headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-disposition")}
            return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=headers,
                                     background=BackgroundTask(r.aclose))
        try:
            content = r.json()
        except ValueError:
            content = error_body("runner_bad_response", "experiment runner returned non-JSON", rid)
        return JSONResponse(status_code=r.status_code, content=content)

    def bad_id(request: Request) -> JSONResponse:
        return JSONResponse(status_code=404, content=error_body("not_found", "unknown experiment or file",
                                                                request.state.request_id))

    @app.get("/api/experiments/{exp_id}/live", tags=["experiments"])
    async def experiment_live(exp_id: str, request: Request):
        if not id_re.match(exp_id):
            return bad_id(request)
        return await to_runner(request, "GET", f"/api/experiments/{exp_id}/live")

    # ------------------------------------------------------------------ gateway views for the dashboard
    gateway_url = settings.gateway_url.rstrip("/")
    gateway_views = {
        "routing/decisions": "/api/routing/decisions",
        "resilience/events": "/api/resilience/events",
        "resilience/breakers": "/api/resilience/breakers",
        "placement/current": "/api/placement/current",
        "dependencies": "/health/dependencies",
    }

    async def to_gateway(request: Request, method: str, path: str):
        rid = request.state.request_id
        body = await request.body() if method == "POST" else None
        try:
            r = await state["http"].request(method, gateway_url + path, content=body,
                                            params=dict(request.query_params), timeout=10.0,
                                            headers={"content-type": "application/json", "X-Request-ID": rid})
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=503, content=error_body(
                "gateway_unreachable", f"gateway not reachable ({type(exc).__name__})", rid))
        try:
            return JSONResponse(status_code=r.status_code, content=r.json())
        except ValueError:
            return JSONResponse(status_code=502, content=error_body("gateway_bad_response", "non-JSON reply", rid))

    @app.get("/api/gateway/{view:path}", tags=["gateway views"])
    async def gateway_view(view: str, request: Request):
        if view not in gateway_views:
            return JSONResponse(status_code=404, content=error_body(
                "not_found", f"gateway view must be one of {sorted(gateway_views)}", request.state.request_id))
        return await to_gateway(request, "GET", gateway_views[view])

    @app.post("/api/gateway/orders", tags=["gateway views"])
    async def gateway_test_order(request: Request):
        return await to_gateway(request, "POST", "/api/orders")

    # ------------------------------------------------------------------ dashboard (static files)
    dash = Path(settings.dashboard_dir)
    if (dash / "index.html").is_file():
        app.mount("/dashboard", StaticFiles(directory=dash, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        if not (dash / "index.html").is_file():
            return JSONResponse(status_code=503, content=error_body(
                "dashboard_missing", f"dashboard files not found in {dash}", request.state.request_id))
        return RedirectResponse("/dashboard/", status_code=307)

    @app.get("/api/scenarios", tags=["experiments"])
    async def scenarios(request: Request):
        return await to_runner(request, "GET", "/api/scenarios")

    @app.get("/api/experiments", tags=["experiments"])
    async def list_experiments(request: Request):
        return await to_runner(request, "GET", "/api/experiments")

    @app.post("/api/experiments", tags=["experiments"])
    async def start_experiment(request: Request):
        return await to_runner(request, "POST", "/api/experiments")

    @app.get("/api/experiments/{exp_id}", tags=["experiments"])
    async def get_experiment(exp_id: str, request: Request):
        if not id_re.match(exp_id):
            return bad_id(request)
        return await to_runner(request, "GET", f"/api/experiments/{exp_id}")

    @app.post("/api/experiments/{exp_id}/stop", tags=["experiments"])
    async def stop_experiment(exp_id: str, request: Request):
        if not id_re.match(exp_id):
            return bad_id(request)
        return await to_runner(request, "POST", f"/api/experiments/{exp_id}/stop")

    @app.get("/api/experiments/{exp_id}/files/{name}", tags=["experiments"])
    async def experiment_file(exp_id: str, name: str, request: Request):
        if not id_re.match(exp_id) or not file_re.match(name):
            return bad_id(request)
        return await to_runner(request, "GET", f"/api/experiments/{exp_id}/files/{name}", stream=True)

    @app.get("/api/services", tags=["health"])
    async def services() -> dict:
        local = state["adapters"]["local-docker"]
        endpoints = await local.discover_endpoints()
        health = await asyncio.gather(*(local.health_check(ep) for ep in endpoints))
        rows = []
        for ep, h in zip(endpoints, health, strict=True):
            rows.append({"service": ep.service, "instance": ep.instance, "env": ep.env, "simulated": ep.simulated,
                         "role": ep.role, "enabled": ep.enabled, "status": h.status, "http_status": h.http_status,
                         "latency_ms": h.latency_ms, "checked_at": h.checked_at, "detail": h.detail})
        return {"services": rows, "placement_version": store.version}

    return app


LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
DASHBOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                 "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
                 "form-action 'self'; frame-ancestors 'none'")


def _hostname(host: str) -> str:
    """Host header without the port: 'a:80' -> 'a', '[::1]:8090' -> '::1', '::1' -> '::1'."""
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def install_browser_protections(app: FastAPI, settings: ControllerSettings) -> None:
    allowed_hosts = {h.strip().lower() for h in settings.allowed_hosts.split(",") if h.strip()}

    @app.middleware("http")
    async def browser_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        if _hostname(host) not in allowed_hosts:
            return JSONResponse(status_code=400, content=error_body(
                "invalid_host", f"Host '{host}' is not allowed (SETU_ALLOWED_HOSTS)", None))
        origin = request.headers.get("origin")
        if request.method in UNSAFE_METHODS and origin is not None:
            parts = urlsplit(origin)
            same_origin = parts.netloc.lower() == host
            local = (parts.hostname or "") in LOCAL_ORIGIN_HOSTS
            if parts.scheme not in ("http", "https") or not (same_origin or local):
                return JSONResponse(status_code=403, content=error_body(
                    "forbidden_origin", f"state-changing requests from origin '{origin}' are refused", None))
        response = await call_next(request)
        if request.url.path.startswith("/dashboard"):
            response.headers["Content-Security-Policy"] = DASHBOARD_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-cache"
        return response
