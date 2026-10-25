"""Controller service (local-development control plane; published on 127.0.0.1 only, no auth).

GET  /api/environments     environments, simulated flags, RTT matrix, mode, per-env adapter info
GET  /api/adapters         every adapter: integration status, per-operation support, reasons
GET  /api/placement        current placement {version, sha256, source, updated_at, placement}
PUT  /api/placement        replace placement after validation (422 envelope on any problem)
POST /api/placement/reset  restore placement.yaml
GET  /api/services         health of the gateway and every placed instance (via the local adapter)

Safety rule for PUT: instance names and URLs are fixed by configs/placement.yaml. The API can
change env labels, roles, weights, enabled flags, routing policy, call mode and resilience
settings, but cannot point the gateway at a new URL.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from setu_x.adapters import build_adapters
from setu_x.adapters.base import ServiceEndpoint
from setu_x.common.app import create_app, error_body
from setu_x.common.config import ConfigError, ControllerSettings
from setu_x.placement import EnvironmentsConfig, PlacementConfig, load_config_dir, parse_placement

log = logging.getLogger("setu_x.controller")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class PlacementStore:
    def __init__(self, envs: EnvironmentsConfig, file_placement: PlacementConfig) -> None:
        self.envs = envs
        self.file_placement = file_placement
        self.current = file_placement
        self.version = 1
        self.source = "file"
        self.updated_at = _now()

    def snapshot(self) -> dict[str, Any]:
        return {"version": self.version, "sha256": self.current.fingerprint(), "source": self.source,
                "updated_at": self.updated_at, "placement": self.current.model_dump()}

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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = client or httpx.AsyncClient(trust_env=False)
        adapters, adapter_errors = build_adapters(envs, store.current, http, environ)
        adapters["local-docker"].extra_endpoints = [
            ServiceEndpoint("gateway", "gateway", settings.env if settings.env in envs.environments else "local",
                            settings.gateway_url, False, "primary", True)
        ]
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
        return store.snapshot()

    @app.post("/api/placement/reset", tags=["placement"])
    async def reset_placement() -> dict:
        store.reset()
        state["adapters"]["local-docker"].update(envs, store.current)
        log.info("placement_reset", extra={"version": store.version})
        return store.snapshot()

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
