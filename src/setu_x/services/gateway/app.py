"""Gateway service.

POST /api/orders {"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"}

Outcomes (see docs/architecture.md §3):
    200 confirmed    inventory and pricing answered
    200 degraded     inventory answered, pricing failed -> list price, price_source="fallback-list-price"
    409 rejected     inventory answered, insufficient stock (business outcome, order-shaped body)
    503 unavailable  inventory failed on every eligible instance, no eligible instance, or the outer
                     deadline expired (error envelope)
    422 invalid      request validation failed (error envelope)

Each dependency call goes through setu_x.services.gateway.resilient: routing decision -> optional
circuit breaker -> per-attempt timeout -> bounded retries with full-jitter backoff -> fallback to
the next routing candidate. In SETU_MODE=simulated the configured link delay between the gateway's
environment and the instance's environment is injected and reported as `injected_delay_ms`.

Other endpoints:
    GET /health/live, /health/ready     standard; ready.info shows placement source + link matrix
    GET /health/dependencies            probes every placed instance's /health/ready (1 s each)
    GET /api/routing/decisions?limit=N  most recent routing decisions (newest first)
    GET /api/placement/current          placement and link matrix this gateway is using right now
    GET /api/resilience/events          retries, fallbacks, breaker transitions/rejections (newest first)
    GET /api/resilience/breakers        current circuit-breaker states
    POST /api/resilience/reset          experiment isolation: sync placement from the controller now,
                                        reset breakers + routing state, reseed the backoff-jitter RNG
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
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
from setu_x.resilience import BreakerRegistry, EventLog
from setu_x.routing import NoEligibleInstance, Router, RoutingDecision
from setu_x.services.gateway.client import Deadline
from setu_x.services.gateway.placement_sync import PlacementSync
from setu_x.services.gateway.resilient import ServiceOutcome, call_with_resilience

log = logging.getLogger("setu_x.gateway")

EVENT_LOG_SIZE = 20_000  # enough for a 300 s run at 100 rps with retries; truncation is reported, never hidden

SIMULATION_NOTICE = ("Environment labels ending in '-simulated' are logical labels on one Docker host, "
                     "not deployments on that cloud provider.")


class OrderRequest(BaseModel):
    sku: str = Field(pattern=SKU_PATTERN, examples=["SKU-001"])
    quantity: int = Field(ge=1, le=100, examples=[2])
    customer_tier: Tier = "standard"


class ResetRequest(BaseModel):
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1,
                             description="new seed for backoff jitter; null = keep the current RNG state")
    sync: bool = Field(default=True, description="fetch the controller placement before resetting")


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
    resilience: dict[str, Any]
    call_mode: str
    environment_mode: str
    placement: dict[str, Any]
    simulated_environments: list[str]
    simulation_notice: str | None


def build_app(
    settings: GatewaySettings,
    client: httpx.AsyncClient | None = None,
    config: tuple[EnvironmentsConfig, PlacementConfig] | None = None,
    clock=time.monotonic,
) -> FastAPI:
    """`client`, `config` and `clock` can be injected by tests; otherwise they are created/loaded here."""
    envs0, initial_placement = config if config is not None else load_config_dir(settings.config_dir)
    if settings.env not in envs0.environments:
        raise ConfigError(f"SETU_ENV: gateway environment '{settings.env}' is not defined in environments.yaml")
    owns_client = client is None
    router = Router()
    events = EventLog(maxlen=EVENT_LOG_SIZE)
    breakers = BreakerRegistry(events, clock=clock)
    rng = random.Random(settings.random_seed)
    simulated_mode = settings.mode == "simulated"
    sync = PlacementSync(envs0, initial_placement, router, None, settings.controller_url,
                         settings.poll_interval_ms / 1000.0)
    sync.on_change.append(lambda reason: breakers.reset(reason))

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

    def info() -> dict[str, Any]:
        return {**sync.info(), "link_delay_injection": "on" if simulated_mode else "off (SETU_MODE is not simulated)"}

    app = create_app(settings, title="SETU-X gateway", readiness=readiness, info=info, lifespan=lifespan)
    app.state.router = router
    app.state.placement_sync = sync
    app.state.events = events
    app.state.breakers = breakers

    def downstream_headers(request: Request) -> dict[str, str]:
        return {"X-Request-ID": request.state.request_id, "traceparent": make_traceparent(request.state.trace_id)}

    def routing_view(decision: RoutingDecision, outcome: ServiceOutcome) -> dict[str, Any]:
        return {**decision.as_dict(), "served_instance": outcome.instance if outcome.ok else None,
                "fallback_used": outcome.fallback_used}

    @app.post(
        "/api/orders",
        response_model=OrderResponse,
        tags=["orders"],
        responses={409: {"description": "insufficient stock"}, 503: {"description": "dependency unavailable"}},
    )
    async def create_order(body: OrderRequest, request: Request):
        t0 = time.perf_counter()
        placement, envs = sync.placement, sync.envs  # snapshot: one order uses one configuration
        deadline = Deadline(placement.resilience.outer_deadline_ms)
        http: httpx.AsyncClient = app.state.http
        rid = request.state.request_id
        common = {
            "headers": downstream_headers(request), "placement": placement, "envs": envs,
            "gateway_env": settings.env, "simulated_mode": simulated_mode, "deadline": deadline,
            "breakers": breakers, "events": events, "rng": rng, "request_id": rid,
        }
        inv_payload = {"sku": body.sku, "quantity": body.quantity}
        price_payload = {"sku": body.sku, "quantity": body.quantity, "customer_tier": body.customer_tier}

        def route(service: str) -> RoutingDecision:
            return router.choose(service, placement.service(service), envs, sync.version)

        price: ServiceOutcome | None = None
        price_route: RoutingDecision | None = None
        try:
            inv_route = route("inventory")
            if placement.call_mode == "parallel":
                price_route = route("pricing")
                inv, price = await asyncio.gather(
                    call_with_resilience(http, service="inventory", path="/v1/stock", payload=inv_payload,
                                         decision=inv_route, **common),
                    call_with_resilience(http, service="pricing", path="/v1/quote", payload=price_payload,
                                         decision=price_route, **common),
                )
            else:
                inv = await call_with_resilience(http, service="inventory", path="/v1/stock", payload=inv_payload,
                                                 decision=inv_route, **common)
                if inv.ok and inv.result.data.get("in_stock"):
                    price_route = route("pricing")
                    price = await call_with_resilience(http, service="pricing", path="/v1/quote",
                                                       payload=price_payload, decision=price_route, **common)
        except NoEligibleInstance as exc:
            return JSONResponse(status_code=503, content=error_body("no_eligible_instance", str(exc), rid))

        total_ms = (time.perf_counter() - t0) * 1000.0
        outcomes = [inv] + ([price] if price is not None else [])
        deps = [o.summary() for o in outcomes]
        routing = [routing_view(inv_route, inv)] + ([routing_view(price_route, price)] if price is not None else [])
        injected = [o.injected_delay_ms for o in outcomes]
        resilience = {
            "attempts": sum(o.attempts for o in outcomes),
            "retries": sum(o.retries for o in outcomes),
            "fallbacks": sum(1 for o in outcomes if o.fallback_used),
            "breaker_rejections": sum(o.breaker_rejections for o in outcomes),
            "injected_delay_ms_total": round(sum(injected), 3),
            # what the injected delay can add to end-to-end latency: max for parallel calls, sum for sequential
            "injected_delay_ms_critical_path": round(
                (max(injected) if placement.call_mode == "parallel" else sum(injected)), 3),
            "settings": {"timeout_ms": placement.resilience.timeout_ms, "retries": placement.resilience.retries,
                         "circuit_breaker": placement.resilience.circuit_breaker.enabled,
                         "fallback": placement.resilience.fallback,
                         "outer_deadline_ms": placement.resilience.outer_deadline_ms},
        }
        used_envs = {(o.env, o.simulated) for o in outcomes}
        simulated = sorted({e for e, s in used_envs if s})
        placement_info = {"version": sync.version, "source": sync.source, "sha256": placement.fingerprint()}

        if not inv.ok:
            log.warning("order_unavailable", extra={"request_id": rid, "error_type": inv.result.error_type,
                                                    "tried": inv.tried})
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "dependency_unavailable",
                    f"inventory unavailable ({inv.result.error_type})",
                    rid,
                    {"dependencies": deps, "routing": routing, "resilience": resilience,
                     "timings_ms": {"total": round(total_ms, 3)}, "placement": placement_info,
                     "environment_mode": settings.mode},
                ),
            )

        data = inv.result.data
        inventory_view = {"available": data.get("available"), "in_stock": data.get("in_stock"),
                          "instance": inv.instance, "env": inv.env, "simulated": inv.simulated,
                          "reported_by": inv.result.served_by}
        base = {
            "order_id": f"ord-{rid[:16]}",
            "request_id": rid,
            "trace_id": request.state.trace_id,
            "sku": body.sku,
            "quantity": body.quantity,
            "customer_tier": body.customer_tier,
            "inventory": inventory_view,
            "timings_ms": {"total": round(total_ms, 3), "inventory": round(inv.elapsed_ms, 3),
                           "pricing": round(price.elapsed_ms, 3) if price is not None else None},
            "dependencies": deps,
            "routing": routing,
            "resilience": resilience,
            "call_mode": placement.call_mode,
            "environment_mode": settings.mode,
            "placement": placement_info,
            "simulated_environments": simulated,
            "simulation_notice": SIMULATION_NOTICE if simulated else None,
        }

        if not data.get("in_stock"):
            return JSONResponse(status_code=409,
                                content=OrderResponse(status="rejected", pricing=None, **base).model_dump())

        if price is not None and price.ok:
            keys = ("unit_price_cents", "discount_pct", "total_cents", "currency")
            pricing_view = {k: price.result.data.get(k) for k in keys}
            pricing_view.update(price_source="pricing-service", instance=price.instance, env=price.env,
                                simulated=price.simulated)
            return OrderResponse(status="confirmed", pricing=pricing_view, **base)

        fallback = list_price_quote(body.sku, body.quantity)
        fallback.update(price_source="fallback-list-price", instance=None, env=None, simulated=None)
        log.warning("order_degraded", extra={"request_id": rid,
                                             "error_type": price.result.error_type if price else "not_called"})
        return OrderResponse(status="degraded", pricing=fallback, **base)

    @app.get("/api/routing/decisions", tags=["routing"])
    async def routing_decisions(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        return {"decisions": router.recent(limit)}

    @app.get("/api/placement/current", tags=["routing"])
    async def placement_current() -> dict:
        return {**sync.info(), "placement": sync.placement.model_dump()}

    @app.get("/api/resilience/events", tags=["resilience"])
    async def resilience_events(limit: int = Query(default=100, ge=1, le=EVENT_LOG_SIZE),
                                type: str | None = Query(default=None, max_length=40),  # noqa: A002
                                since: int | None = Query(default=None, ge=0)) -> dict:
        return {"events": events.recent(limit, type, since), "last_seq": events.last_seq,
                "oldest_seq": events.oldest_seq}

    @app.post("/api/resilience/reset", tags=["resilience"])
    async def resilience_reset(body: ResetRequest | None = None) -> dict:
        """Start an experiment run from a known state. Does not touch placement or faults themselves."""
        body = body or ResetRequest()
        synced = await sync.sync_once() if body.sync else False
        breakers.reset("reset via API")
        router.reset_state()
        if body.seed is not None:
            rng.seed(body.seed)
        events.add("resilience_reset", seed=body.seed, placement_version=sync.version, synced=synced)
        return {"synced": synced, "seed": body.seed, "events_last_seq": events.last_seq, **sync.info()}

    @app.get("/api/resilience/breakers", tags=["resilience"])
    async def resilience_breakers() -> dict:
        cb = sync.placement.resilience.circuit_breaker
        return {"enabled": cb.enabled, "breakers": breakers.snapshot()}

    @app.get("/health/dependencies", tags=["health"])
    async def dependencies() -> dict:
        http: httpx.AsyncClient = app.state.http
        placement, envs = sync.placement, sync.envs

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
