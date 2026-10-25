"""Pricing service.

POST /v1/quote {"sku": "SKU-001", "quantity": 2, "customer_tier": "gold"}
 -> 200 {"sku", "quantity", "customer_tier", "unit_price_cents", "discount_pct", "total_cents",
         "currency", "instance", "env", "request_id"}
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from setu_x.common.app import create_app
from setu_x.common.config import ServiceSettings
from setu_x.domain import SKU_PATTERN, Tier, quote


class QuoteRequest(BaseModel):
    sku: str = Field(pattern=SKU_PATTERN)
    quantity: int = Field(ge=1, le=100)
    customer_tier: Tier = "standard"


class QuoteResponse(BaseModel):
    sku: str
    quantity: int
    customer_tier: Tier
    unit_price_cents: int
    discount_pct: int
    total_cents: int
    currency: str
    instance: str
    env: str
    request_id: str


def build_app(settings: ServiceSettings) -> FastAPI:
    app = create_app(settings, title="SETU-X pricing")

    @app.post("/v1/quote", response_model=QuoteResponse, tags=["pricing"])
    async def get_quote(body: QuoteRequest, request: Request) -> QuoteResponse:
        q = quote(body.sku, body.quantity, body.customer_tier)
        return QuoteResponse(
            sku=body.sku,
            quantity=body.quantity,
            customer_tier=body.customer_tier,
            instance=settings.instance,
            env=settings.env,
            request_id=request.state.request_id,
            **q,
        )

    return app
