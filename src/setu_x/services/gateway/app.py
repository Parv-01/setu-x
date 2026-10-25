"""Gateway service.

POST /api/orders {"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"}

Outcomes (see docs/architecture.md §3):
    200 confirmed    inventory and pricing answered
    200 degraded     inventory answered, pricing failed -> list price, price_source="fallback-list-price"
    409 rejected     inventory answered, insufficient stock (business outcome, order-shaped body)
    503 unavailable  inventory failed, no eligible instance, or outer deadline expired (error envelope)
    422 invalid      request validation failed (error envelope)

Which instance serves each call is decided by the routing policy in the current placement
(configs/placement.yaml, kept in sync with the controller). Every response lists the routing
decisions and marks environments that are simulation labels.

Other endpoints:
    GET /health/live, /health/ready     standard; ready.info shows the placement source
    GET /health/dependencies            probes every placed instance's /health/ready (1 s each)
    GET /api/routing/decisions?limit=N  most recent routing decisions (newest first)
    GET /api/placement/current          the placement this gateway is using right now
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from setu_x.common.app import create_app, error_body
from setu_x.common.config import ConfigError, GatewaySettings
from setu_x.common.ids import make_traceparent
from setu_x.domain import SKU_PATTERN, Tier, list_price_quote
from setu_x.placement import EnvironmentsConfig, PlacementConfig, load_config_dir
from setu_x.routing import NoEligibleInstance, Router, RoutingDecision
from setu_x.services.gateway.client import CallResult, Deadline, call_json
from setu_x.services.gateway.placement_sync import PlacementSync

log = logging.getLogger("setu_x.gateway")

SIMULATION_NOTICE = ("Environment labels ending in '-simulated' are logical labels on one Docker host, "
                     "not deployments on that cloud provider.")


class OrderRequest(BaseModel):
    sku: str = Field(pattern=SKU_PATTERN, examples=["SKU-001"])
    quantity: int = Field(ge=1, le=100, examples=[2])
    customer_tier: Tier = "standard"


class OrderResponse(BaseModel):
    order_id: str
    request_id: str
    trace_id: str
    status: Literal["confirmed", "degraded", "rejected"]
    sku: str
    quantity: int
    customer_tier: Tier
    inventory: dict[str, Any]
    pricing: dict[str, Any] | None
    timings_ms: dict[str, float | None]
    dependencies: list[dict[str, Any]]
    routing: list[dict[str, Any]]
    call_mode: str
    environment_mode: str
    placement: dict[str, Any]
    simulated_environments: list[str]
    simulation_notice: str | None


def build_app(
    settings: GatewaySettings,
    client: httpx.AsyncClient | None = None,
    config: tuple[EnvironmentsConfig, PlacementConfig] | None = None,
) -> FastAPI:
    """`client` and `config` can be injected by tests; otherwise they are created/loaded here."""
    envs, initial_placement = config if config is not None else load_config_dir(settings.config_dir)
    if settings.env not in envs.environments:
        raise ConfigError(f"SETU_ENV: gateway environment '{settings.env}' is not defined in environments.yaml")
    owns_client = client is None
    router = Router()
    sync = PlacementSync(envs, initial_placement, router, None, settings.controller_url,
                         settings.poll_interval_ms / 1000.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            trust_env=False,  # never route inter-service calls through a host proxy
        )
        sync.client = app.state.http
        task = asyncio.create_task(sync.run()) if settings.controller_url else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if owns_client:
            await app.state.http.aclose()

    def readiness() -> dict[str, str]:
        http = getattr(app.state, "http", None)
        return {"config": "ok", "http_client": "ok" if http is not None and not http.is_closed else "not_started"}

    app = create_app(settings, title="SETU-X gateway", readiness=readiness, info=sync.info, lifespan=lifespan)
    app.state.router = router
    app.state.placement_sync = sync

    def downstream_headers(request: Request) -> dict[str, str]:
        return {"X-Request-ID": request.state.request_id, "traceparent": make_traceparent(request.state.trace_id)}

    def dep_summary(result: CallResult, decision: RoutingDecision) -> dict[str, Any]:
        return {**result.summary(), "instance": decision.instance, "env": decision.env,
                "simulated": decision.simulated}

    @app.post(
        "/api/orders",
        response_model=OrderResponse,
        tags=["orders"],
        responses={409: {"description": "insufficient stock"}, 503: {"description": "dependency unavailable"}},
    )
    async def create_order(body: OrderRequest, request: Request):
        t0 = time.perf_counter()
        placement = sync.placement  # snapshot: one order uses one placement version
        res = placement.resilience
        deadline = Deadline(res.outer_deadline_ms)
        headers = downstream_headers(request)
        http: httpx.AsyncClient = app.state.http
        rid = request.state.request_id
        common = {"timeout_ms": res.timeout_ms, "connect_timeout_ms": res.connect_timeout_ms, "deadline": deadline}

        inv_payload = {"sku": body.sku, "quantity": body.quantity}
        price_payload = {"sku": body.sku, "quantity": body.quantity, "customer_tier": body.customer_tier}

        def route(service: str) -> RoutingDecision:
            return router.choose(service, placement.service(service), envs, sync.version)

        # A pricing route is only chosen (and logged) when pricing is actually called.
        price: CallResult | None = None
        price_route: RoutingDecision | None = None
        try:
            inv_route = route("inventory")
            inv_url = inv_route.url.rstrip("/") + "/v1/stock"
            if placement.call_mode == "parallel":
                price_route = route("pricing")
                inv, price = await asyncio.gather(
                    call_json(http, "inventory", inv_url, inv_payload, headers, **common),
                    call_json(http, "pricing", price_route.url.rstrip("/") + "/v1/quote", price_payload,
                              headers, **common),
                )
            else:
                inv = await call_json(http, "inventory", inv_url, inv_payload, headers, **common)
                if inv.ok and inv.data.get("in_stock"):
                    price_route = route("pricing")
                    price = await call_json(http, "pricing", price_route.url.rstrip("/") + "/v1/quote",
                                            price_payload, headers, **common)
        except NoEligibleInstance as exc:
            return JSONResponse(status_code=503, content=error_body("no_eligible_instance", str(exc), rid))

        total_ms = (time.perf_counter() - t0) * 1000.0
        deps = [dep_summary(inv, inv_route)] + ([dep_summary(price, price_route)] if price is not None else [])
        routes_used = [inv_route] + ([price_route] if price_route is not None else [])
        routing = [d.as_dict() for d in routes_used]
        simulated = sorted({d.env for d in routes_used if d.simulated})
        placement_info = {"version": sync.version, "source": sync.source, "sha256": placement.fingerprint()}

        if not inv.ok:
            log.warning("order_unavailable", extra={"request_id": rid, "error_type": inv.error_type,
                                                    "instance": inv_route.instance})
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "dependency_unavailable",
                    f"inventory unavailable ({inv.error_type})",
                    rid,
                    {"dependencies": deps, "routing": routing, "timings_ms": {"total": round(total_ms, 3)},
                     "placement": placement_info},
                ),
            )

        inventory_view = {
            "available": inv.data.get("available"),
            "in_stock": inv.data.get("in_stock"),
            "instance": inv_route.instance,
            "env": inv_route.env,
            "simulated": inv_route.simulated,
            "reported_by": inv.served_by,
        }
        base = {
            "order_id": f"ord-{rid[:16]}",
            "request_id": rid,
            "trace_id": request.state.trace_id,
            "sku": body.sku,
            "quantity": body.quantity,
            "customer_tier": body.customer_tier,
            "inventory": inventory_view,
            "timings_ms": {
                "total": round(total_ms, 3),
                "inventory": round(inv.elapsed_ms, 3),
                "pricing": round(price.elapsed_ms, 3) if price is not None else None,
            },
            "dependencies": deps,
            "routing": routing,
            "call_mode": placement.call_mode,
            "environment_mode": settings.mode,
            "placement": placement_info,
            "simulated_environments": simulated,
            "simulation_notice": SIMULATION_NOTICE if simulated else None,
        }

        if not inv.data.get("in_stock"):
            return JSONResponse(status_code=409,
                                content=OrderResponse(status="rejected", pricing=None, **base).model_dump())

        if price is not None and price.ok:
            keys = ("unit_price_cents", "discount_pct", "total_cents", "currency")
            pricing_view = {k: price.data.get(k) for k in keys}
            pricing_view.update(price_source="pricing-service", instance=price_route.instance, env=price_route.env,
                                simulated=price_route.simulated)
            return OrderResponse(status="confirmed", pricing=pricing_view, **base)

        fallback = list_price_quote(body.sku, body.quantity)
        fallback.update(price_source="fallback-list-price", instance=None, env=None, simulated=None)
        log.warning("order_degraded", extra={"request_id": rid,
                                             "error_type": price.error_type if price else "not_called"})
        return OrderResponse(status="degraded", pricing=fallback, **base)

    @app.get("/api/routing/decisions", tags=["routing"])
    async def routing_decisions(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        return {"decisions": router.recent(limit)}

    @app.get("/api/placement/current", tags=["routing"])
    async def placement_current() -> dict:
        return {**sync.info(), "placement": sync.placement.model_dump()}

    @app.get("/health/dependencies", tags=["health"])
    async def dependencies() -> dict:
        http: httpx.AsyncClient = app.state.http
        placement = sync.placement

        async def probe(service: str, inst) -> dict:
            start = time.perf_counter()
            try:
                r = await http.get(inst.url.rstrip("/") + "/health/ready", timeout=1.0)
                status = "ready" if r.status_code == 200 else f"http_{r.status_code}"
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            return {"service": service, "instance": inst.name, "env": inst.env,
                    "simulated": envs.is_simulated(inst.env), "enabled": inst.enabled, "status": status,
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}

        results = await asyncio.gather(*(probe(s, i) for s, svc in placement.service_items() for i in svc.instances))
        return {"dependencies": list(results)}

    return app
