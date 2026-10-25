"""Shared test fixtures.

Integration tests run the real gateway, controller, inventory and pricing FastAPI apps in-process.
`HostRouter` sends each outgoing request to the ASGI app registered for its host name. An
unregistered host raises httpx.ConnectError, which is what a stopped container produces.

Placement for tests starts from the real configs/ files and applies per-test overrides, so the
shipped configuration is exercised by every gateway test.
"""
from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from setu_x.common.config import ControllerSettings, GatewaySettings, ServiceSettings
from setu_x.placement import EnvironmentsConfig, PlacementConfig, parse_environments, parse_placement
from setu_x.services.controller.app import build_app as build_controller
from setu_x.services.gateway.app import build_app as build_gateway
from setu_x.services.inventory.app import build_app as build_inventory
from setu_x.services.pricing.app import build_app as build_pricing

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"


def raw_config() -> tuple[dict, dict]:
    envs = yaml.safe_load((CONFIG_DIR / "environments.yaml").read_text(encoding="utf-8"))
    placement = yaml.safe_load((CONFIG_DIR / "placement.yaml").read_text(encoding="utf-8"))
    return envs, placement


def build_config(
    *,
    call_mode: str | None = None,
    resilience: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    pricing: dict[str, Any] | None = None,
    **resilience_kw: Any,
) -> tuple[EnvironmentsConfig, PlacementConfig]:
    """Shipped config with overrides. Keyword args like timeout_ms=... go into `resilience`."""
    env_raw, pl_raw = raw_config()
    pl = copy.deepcopy(pl_raw)
    if call_mode:
        pl["call_mode"] = call_mode
    pl["resilience"].update(resilience or {})
    pl["resilience"].update(resilience_kw)
    if inventory:
        pl["services"]["inventory"].update(inventory)
    if pricing:
        pl["services"]["pricing"].update(pricing)
    envs = parse_environments(env_raw, "test environments")
    return envs, parse_placement(pl, envs, "test placement")


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

    def hosts(self, path_prefix: str = "/v1/") -> list[str]:
        return [r.url.host for r in self.requests if r.url.path.startswith(path_prefix)]


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


def default_routes() -> dict[str, FastAPI]:
    return {
        "inventory-a": inventory_app("inventory-a", "aws-simulated"),
        "inventory-b": inventory_app("inventory-b", "gcp-simulated"),
        "pricing": pricing_app(),
    }


DEFAULT_TEST_RESILIENCE = {"timeout_ms": 300, "connect_timeout_ms": 100, "outer_deadline_ms": 1000}


@pytest.fixture
def make_gateway():
    """async with make_gateway(routes={...}, call_mode=..., timeout_ms=..., config=...) as (client, router)"""

    @asynccontextmanager
    async def _make(routes: dict[str, FastAPI] | None = None, config=None, controller_url: str = "",
                    **overrides: Any) -> AsyncIterator:
        if routes is None:
            routes = default_routes()
        if config is None:
            res = {**DEFAULT_TEST_RESILIENCE, **{k: v for k, v in overrides.items() if k != "call_mode"}}
            config = build_config(call_mode=overrides.get("call_mode"), resilience=res)
        router = HostRouter({host: asgi(app) for host, app in routes.items()})
        downstream = httpx.AsyncClient(transport=router)
        settings = GatewaySettings(service_name="gateway", instance="gateway", env="local",
                                   controller_url=controller_url, config_dir=str(CONFIG_DIR))
        app = build_gateway(settings, client=downstream, config=config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=asgi(app), base_url="http://gateway") as client:
                client.app = app  # type: ignore[attr-defined]  # tests may inspect app.state
                yield client, router
        await downstream.aclose()

    return _make


@pytest.fixture
def make_controller():
    @asynccontextmanager
    async def _make(routes: dict[str, FastAPI] | None = None, environ: dict[str, str] | None = None,
                    config=None) -> AsyncIterator:
        router = HostRouter({host: asgi(app) for host, app in (routes or {}).items()})
        downstream = httpx.AsyncClient(transport=router)
        settings = ControllerSettings(service_name="controller", instance="controller", env="local",
                                      config_dir=str(CONFIG_DIR))
        app = build_controller(settings, client=downstream, config=config or build_config(), environ=environ or {})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=asgi(app), base_url="http://controller") as client:
                client.app = app  # type: ignore[attr-defined]
                yield client
        await downstream.aclose()

    return _make
