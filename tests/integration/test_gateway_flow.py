"""Gateway order flow with real downstream apps in-process (healthy and unhealthy paths)."""
import time

import pytest

from setu_x import domain
from tests.conftest import failing_app, hanging_app, inventory_app, pricing_app

ORDER = {"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"}


@pytest.mark.parametrize("call_mode", ["parallel", "sequential"])
async def test_confirmed_order_expected_result(make_gateway, call_mode):
    async with make_gateway(call_mode=call_mode) as (client, router):
        r = await client.post("/api/orders", json=ORDER, headers={"X-Request-ID": "e2e-req-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed" and body["call_mode"] == call_mode
    assert body["inventory"] == {"available": 8, "in_stock": True, "instance": "inventory-a", "env": "aws-simulated",
                                 "simulated": True, "reported_by": "inventory-a@aws-simulated"}
    assert body["simulated_environments"] == ["aws-simulated", "azure-simulated"] and body["simulation_notice"]
    assert [d["instance"] for d in body["routing"]] == ["inventory-a", "pricing"]
    assert body["pricing"]["total_cents"] == 11000 and body["pricing"]["price_source"] == "pricing-service"
    assert body["request_id"] == "e2e-req-1" and body["order_id"] == "ord-e2e-req-1"
    # request id is propagated to every downstream call
    assert {req.headers["x-request-id"] for req in router.requests} == {"e2e-req-1"}
    assert all(req.headers["traceparent"].split("-")[1] == body["trace_id"] for req in router.requests)
    assert body["timings_ms"]["total"] >= max(body["timings_ms"]["inventory"], body["timings_ms"]["pricing"])


async def test_gold_tier_discount_flows_through(make_gateway):
    async with make_gateway() as (client, _):
        r = await client.post("/api/orders", json={**ORDER, "customer_tier": "gold"})
    assert r.json()["pricing"]["total_cents"] == domain.quote("SKU-001", 2, "gold")["total_cents"] == 9900


async def test_insufficient_stock_is_409_rejected(make_gateway):
    async with make_gateway() as (client, _):
        r = await client.post("/api/orders", json={**ORDER, "quantity": 9})  # SKU-001 has 8
    assert r.status_code == 409 and r.json()["status"] == "rejected" and r.json()["pricing"] is None


async def test_sequential_mode_skips_pricing_when_out_of_stock(make_gateway):
    async with make_gateway(call_mode="sequential") as (client, router):
        await client.post("/api/orders", json={**ORDER, "quantity": 9})
    assert [req.url.host for req in router.requests] == ["inventory-a"]


async def test_invalid_order_is_422_envelope(make_gateway):
    async with make_gateway() as (client, router):
        r = await client.post("/api/orders", json={"sku": "SKU-1", "quantity": 2})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert router.requests == []  # nothing reaches downstream


async def test_inventory_down_gives_bounded_503(make_gateway):
    async with make_gateway(routes={"pricing": pricing_app()}) as (client, _):  # inventory not routed
        t0 = time.perf_counter()
        r = await client.post("/api/orders", json=ORDER)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "dependency_unavailable" and "connect_error" in err["message"]
    assert elapsed < 0.5


async def test_inventory_hang_is_cut_by_timeout(make_gateway):
    routes = {"inventory-a": hanging_app(30), "pricing": pricing_app()}
    async with make_gateway(routes=routes, timeout_ms=200, outer_deadline_ms=1000) as (client, _):
        t0 = time.perf_counter()
        r = await client.post("/api/orders", json=ORDER)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 503 and "timeout" in r.json()["error"]["message"]
    assert 0.2 <= elapsed < 0.2 + 0.3, elapsed


async def test_outer_deadline_bounds_sequential_calls(make_gateway):
    # Sequential mode with a per-call timeout near the deadline: inventory is slow but answers, and
    # pricing gets only what remains of the outer deadline.
    routes = {"inventory-a": inventory_app(), "pricing": hanging_app(30)}
    async with make_gateway(routes=routes, call_mode="sequential", timeout_ms=400, outer_deadline_ms=500) as (
        client,
        _,
    ):
        t0 = time.perf_counter()
        r = await client.post("/api/orders", json=ORDER)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and r.json()["status"] == "degraded"
    assert elapsed < 0.5 + 0.3, elapsed


@pytest.mark.parametrize("pricing", [None, "500", "hang"])
async def test_pricing_failure_degrades_with_list_price(make_gateway, pricing):
    routes = {"inventory-a": inventory_app()}
    if pricing == "500":
        routes["pricing"] = failing_app(500)
    elif pricing == "hang":
        routes["pricing"] = hanging_app(30)
    async with make_gateway(routes=routes) as (client, _):
        r = await client.post("/api/orders", json={**ORDER, "customer_tier": "gold"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["pricing"]["price_source"] == "fallback-list-price"
    assert body["pricing"]["total_cents"] == domain.list_price_quote("SKU-001", 2)["total_cents"] == 11000
    pricing_dep = next(d for d in body["dependencies"] if d["name"] == "pricing")
    assert pricing_dep["ok"] is False and pricing_dep["error_type"] in {"connect_error", "http_5xx", "timeout"}


async def test_gateway_health_and_dependency_probe(make_gateway):
    async with make_gateway(routes={"inventory-a": inventory_app()}) as (client, _):
        ready = (await client.get("/health/ready")).json()
        deps = (await client.get("/health/dependencies")).json()["dependencies"]
    assert ready["status"] == "ready" and ready["checks"] == {"config": "ok", "http_client": "ok"}
    assert ready["info"]["placement_source"] == "file"
    status = {d["instance"]: d["status"] for d in deps}
    assert status == {"inventory-a": "ready", "inventory-b": "ConnectError", "pricing": "ConnectError"}
    assert all(d["simulated"] for d in deps)
