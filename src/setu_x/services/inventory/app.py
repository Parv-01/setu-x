"""Inventory service.

POST /v1/stock {"sku": "SKU-001", "quantity": 2}
 -> 200 {"sku", "requested", "available", "in_stock", "instance", "env", "request_id"}
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field

from setu_x.common.app import create_app
from setu_x.common.config import ServiceSettings
from setu_x.domain import SKU_PATTERN, stock_for
from setu_x.faults import install_fault_injection


class StockRequest(BaseModel):
    sku: str = Field(pattern=SKU_PATTERN)
    quantity: int = Field(ge=1, le=100)


class StockResponse(BaseModel):
    sku: str
    requested: int
    available: int
    in_stock: bool
    instance: str
    env: str
    request_id: str


def build_app(settings: ServiceSettings) -> FastAPI:
    app = create_app(settings, title="SETU-X inventory")
    fault_guard = install_fault_injection(app, settings.instance)

    @app.post("/v1/stock", response_model=StockResponse, tags=["inventory"], dependencies=[Depends(fault_guard)])
    async def check_stock(body: StockRequest, request: Request) -> StockResponse:
        available = stock_for(body.sku)
        return StockResponse(
            sku=body.sku,
            requested=body.quantity,
            available=available,
            in_stock=available >= body.quantity,
            instance=settings.instance,
            env=settings.env,
            request_id=request.state.request_id,
        )

    return app
