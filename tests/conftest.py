"""Shared test fixtures.

Integration tests run the real gateway, inventory and pricing FastAPI apps in-process.
`HostRouter` sends each outgoing gateway request to the ASGI app registered for its host name.
An unregistered host raises httpx.ConnectError, which is what a stopped container produces.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from setu_x.common.config import GatewaySettings, ServiceSettings
from setu_x.services.gateway.app import build_app as build_gateway
from setu_x.services.inventory.app import build_app as build_inventory
from setu_x.services.pricing.app import build_app as build_pricing


class HostRouter(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        transport = self.routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no route to {request.url.host}", request=request)
        return await transport.handle_async_request(request)


def asgi(app: FastAPI) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


def inventory_app(instance: str = "inventory-a", env: str = "aws-simulated") -> FastAPI:
    return build_inventory(ServiceSettings(service_name="inventory", instance=instance, env=env))


def pricing_app(instance: str = "pricing", env: str = "azure-simulated") -> FastAPI:
    return build_pricing(ServiceSettings(service_name="pricing", instance=instance, env=env))


def hanging_app(seconds: float = 30.0) -> FastAPI:
    """A dependency that accepts the request and never answers in time."""
    app = FastAPI()

    @app.post("/{path:path}")
    async def hang(path: str):
        await asyncio.sleep(seconds)
        return {}

    return app


def failing_app(status: int = 500) -> FastAPI:
    app = FastAPI()

    @app.post("/{path:path}")
    async def fail(path: str):
        return JSONResponse(status_code=status, content={"error": "injected"})

    return app


def gateway_settings(**overrides) -> GatewaySettings:
    base = dict(
        service_name="gateway",
        instance="gateway",
        env="local",
        inventory_url="http://inventory-a:8000",
        pricing_url="http://pricing:8000",
        timeout_ms=300,
        connect_timeout_ms=100,
        outer_deadline_ms=1000,
    )
    base.update(overrides)
    return GatewaySettings(**base)


@pytest.fixture
def make_gateway():
    """Usage: async with make_gateway(routes={...}, **settings) as (client, router): ..."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _make(routes: dict[str, FastAPI] | None = None, **settings) -> AsyncIterator:
        if routes is None:
            routes = {"inventory-a": inventory_app(), "pricing": pricing_app()}
        router = HostRouter({host: asgi(app) for host, app in routes.items()})
        downstream = httpx.AsyncClient(transport=router)
        app = build_gateway(gateway_settings(**settings), client=downstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=asgi(app), base_url="http://gateway") as client:
                yield client, router
        await downstream.aclose()

    return _make
