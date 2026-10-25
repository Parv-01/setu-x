"""Inventory and pricing services in-process: endpoints, validation envelope, health semantics."""
import httpx

from setu_x import domain
from tests.conftest import asgi, inventory_app, pricing_app


async def client_for(app):
    return httpx.AsyncClient(transport=asgi(app), base_url="http://svc")


async def test_inventory_stock_check_is_deterministic():
    async with await client_for(inventory_app()) as c:
        r = await c.post("/v1/stock", json={"sku": "SKU-001", "quantity": 2}, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] == domain.stock_for("SKU-001") == 8
    assert body["in_stock"] is True and body["instance"] == "inventory-a" and body["env"] == "aws-simulated"
    assert body["request_id"] == "req-1" and r.headers["x-request-id"] == "req-1"
    assert r.headers["x-served-by"] == "inventory-a@aws-simulated"


async def test_pricing_quote_matches_domain():
    async with await client_for(pricing_app()) as c:
        r = await c.post("/v1/quote", json={"sku": "SKU-001", "quantity": 2, "customer_tier": "gold"})
    assert r.status_code == 200
    body = r.json()
    for k, v in domain.quote("SKU-001", 2, "gold").items():
        assert body[k] == v
    assert body["total_cents"] == 9900


async def test_validation_errors_use_the_error_envelope():
    async with await client_for(inventory_app()) as c:
        for payload in ({"sku": "sku-1", "quantity": 1}, {"sku": "SKU-001", "quantity": 0},
                        {"sku": "SKU-001", "quantity": 101}, {"quantity": 1}):
            r = await c.post("/v1/stock", json=payload)
            assert r.status_code == 422, payload
            err = r.json()["error"]
            assert err["code"] == "invalid_request" and err["request_id"] and err["details"]
        r = await c.post("/v1/stock", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 422


async def test_unknown_route_uses_error_envelope():
    async with await client_for(pricing_app()) as c:
        r = await c.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_health_endpoints_documented_shape():
    async with await client_for(inventory_app("inventory-b", "gcp-simulated")) as c:
        live = await c.get("/health/live")
        ready = await c.get("/health/ready")
    assert live.status_code == 200 and live.json()["status"] == "alive"
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready" and body["checks"] == {"config": "ok"}
    for key in ("service", "instance", "env", "mode", "version"):
        assert key in body
    assert body["instance"] == "inventory-b" and body["env"] == "gcp-simulated"


async def test_traceparent_is_propagated_when_valid_and_replaced_when_not():
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    async with await client_for(inventory_app()) as c:
        r1 = await c.get("/health/live", headers={"traceparent": tp})
        r2 = await c.get("/health/live", headers={"traceparent": "bogus"})
    assert r1.headers["traceparent"].split("-")[1] == "a" * 32
    assert r2.headers["traceparent"].split("-")[1] != "a" * 32
