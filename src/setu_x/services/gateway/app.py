"""Gateway service.

POST /api/orders {"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"}

Outcomes (see docs/architecture.md §3):
    200 confirmed    inventory and pricing answered
    200 degraded     inventory answered, pricing failed -> list price, price_source="fallback-list-price"
    409 rejected     inventory answered, insufficient stock (business outcome, order-shaped body)
    503 unavailable  inventory failed or the outer deadline expired (error envelope)
    422 invalid      request validation failed (error envelope)

Other endpoints:
    GET /health/live, /health/ready     (standard, see setu_x.common.app)
    GET /health/dependencies            probes each dependency's /health/ready (1 s timeout each)
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from setu_x.common.app import create_app, error_body
from setu_x.common.config import GatewaySettings
from setu_x.common.ids import make_traceparent
from setu_x.domain import SKU_PATTERN, Tier, list_price_quote
from setu_x.services.gateway.client import CallResult, Deadline, call_json

log = logging.getLogger("setu_x.gateway")


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
    call_mode: str
    environment_mode: str


def build_app(settings: GatewaySettings, client: httpx.AsyncClient | None = None) -> FastAPI:
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            trust_env=False,  # never route inter-service calls through a host proxy
        )
        yield
        if owns_client:
            await app.state.http.aclose()

    def readiness() -> dict[str, str]:
        http = getattr(app.state, "http", None)
        return {"config": "ok", "http_client": "ok" if http is not None and not http.is_closed else "not_started"}

    app = create_app(settings, title="SETU-X gateway", readiness=readiness, lifespan=lifespan)

    def downstream_headers(request: Request) -> dict[str, str]:
        return {
            "X-Request-ID": request.state.request_id,
            "traceparent": make_traceparent(request.state.trace_id),
        }

    @app.post(
        "/api/orders",
        response_model=OrderResponse,
        tags=["orders"],
        responses={409: {"description": "insufficient stock"}, 503: {"description": "dependency unavailable"}},
    )
    async def create_order(body: OrderRequest, request: Request):
        t0 = time.perf_counter()
        deadline = Deadline(settings.outer_deadline_ms)
        headers = downstream_headers(request)
        http: httpx.AsyncClient = app.state.http
        common = {
            "timeout_ms": settings.timeout_ms,
            "connect_timeout_ms": settings.connect_timeout_ms,
            "deadline": deadline,
        }
        inv_payload = {"sku": body.sku, "quantity": body.quantity}
        price_payload = {"sku": body.sku, "quantity": body.quantity, "customer_tier": body.customer_tier}
        inv_url = settings.inventory_url.rstrip("/") + "/v1/stock"
        price_url = settings.pricing_url.rstrip("/") + "/v1/quote"

        price: CallResult | None
        if settings.call_mode == "parallel":
            inv, price = await asyncio.gather(
                call_json(http, "inventory", inv_url, inv_payload, headers, **common),
                call_json(http, "pricing", price_url, price_payload, headers, **common),
            )
        else:
            inv = await call_json(http, "inventory", inv_url, inv_payload, headers, **common)
            price = None
            if inv.ok and inv.data.get("in_stock"):
                price = await call_json(http, "pricing", price_url, price_payload, headers, **common)

        total_ms = (time.perf_counter() - t0) * 1000.0
        deps = [r.summary() for r in (inv, price) if r is not None]
        rid = request.state.request_id

        if not inv.ok:
            log.warning("order_unavailable", extra={"request_id": rid, "error_type": inv.error_type})
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "dependency_unavailable",
                    f"inventory unavailable ({inv.error_type})",
                    rid,
                    {"dependencies": deps, "timings_ms": {"total": round(total_ms, 3)}},
                ),
            )

        inventory_view = {
            "available": inv.data.get("available"),
            "in_stock": inv.data.get("in_stock"),
            "instance": inv.data.get("instance"),
            "env": inv.data.get("env"),
        }
        timings = {
            "total": round(total_ms, 3),
            "inventory": round(inv.elapsed_ms, 3),
            "pricing": round(price.elapsed_ms, 3) if price is not None else None,
        }
        base = {
            "order_id": f"ord-{rid[:16]}",
            "request_id": rid,
            "trace_id": request.state.trace_id,
            "sku": body.sku,
            "quantity": body.quantity,
            "customer_tier": body.customer_tier,
            "inventory": inventory_view,
            "timings_ms": timings,
            "dependencies": deps,
            "call_mode": settings.call_mode,
            "environment_mode": settings.mode,
        }

        if not inv.data.get("in_stock"):
            return JSONResponse(
                status_code=409, content=OrderResponse(status="rejected", pricing=None, **base).model_dump()
            )

        if price is not None and price.ok:
            pricing_view = {
                k: price.data.get(k) for k in ("unit_price_cents", "discount_pct", "total_cents", "currency")
            }
            pricing_view.update(price_source="pricing-service", instance=price.data.get("instance"))
            return OrderResponse(status="confirmed", pricing=pricing_view, **base)

        fallback = list_price_quote(body.sku, body.quantity)
        fallback.update(price_source="fallback-list-price", instance=None)
        log.warning(
            "order_degraded",
            extra={"request_id": rid, "error_type": price.error_type if price else "not_called"},
        )
        return OrderResponse(status="degraded", pricing=fallback, **base)

    @app.get("/health/dependencies", tags=["health"])
    async def dependencies() -> dict:
        http: httpx.AsyncClient = app.state.http

        async def probe(name: str, base_url: str) -> dict:
            start = time.perf_counter()
            try:
                r = await http.get(base_url.rstrip("/") + "/health/ready", timeout=1.0)
                status = "ready" if r.status_code == 200 else f"http_{r.status_code}"
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            return {"name": name, "url": base_url, "status": status,
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}

        results = await asyncio.gather(
            probe("inventory", settings.inventory_url), probe("pricing", settings.pricing_url)
        )
        return {"dependencies": list(results)}

    return app
