"""Fault injection on a real service app, and the controller's allow-listed fault proxy."""
import time

import httpx

from tests.conftest import asgi, default_routes, inventory_app

STOCK = {"sku": "SKU-001", "quantity": 1}


async def client_for(app):
    return httpx.AsyncClient(transport=asgi(app), base_url="http://svc")


async def test_error_fault_returns_envelope_with_request_id_and_health_unaffected():
    app = inventory_app()
    async with await client_for(app) as c:
        r = await c.post("/admin/fault", json={"mode": "error", "duration_s": 30, "error_status": 502})
        assert r.status_code == 200 and r.json()["active"]["mode"] == "error"
        r = await c.post("/v1/stock", json=STOCK, headers={"X-Request-ID": "f-1"})
        assert r.status_code == 502
        assert r.json()["error"] == {"code": "injected_fault", "message": "injected error on inventory-a",
                                     "request_id": "f-1"}
        assert r.headers["x-request-id"] == "f-1"  # still passes through the normal middleware
        assert (await c.get("/health/ready")).status_code == 200
        await c.delete("/admin/fault")
        assert (await c.post("/v1/stock", json=STOCK)).status_code == 200


async def test_slow_fault_adds_delay():
    app = inventory_app()
    async with await client_for(app) as c:
        await c.post("/admin/fault", json={"mode": "slow", "duration_s": 30, "delay_ms": 150})
        t0 = time.perf_counter()
        r = await c.post("/v1/stock", json=STOCK)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and elapsed >= 0.15


async def test_hang_fault_ends_with_503_when_fault_expires():
    app = inventory_app()
    async with await client_for(app) as c:
        await c.post("/admin/fault", json={"mode": "hang", "duration_s": 1})
        t0 = time.perf_counter()
        r = await c.post("/v1/stock", json=STOCK)
        elapsed = time.perf_counter() - t0
        info = (await c.get("/admin/fault")).json()
    assert r.status_code == 503 and "hang" in r.json()["error"]["message"]
    assert 0.9 <= elapsed < 1.5
    assert info["active"] is None and any(e["type"] == "fault_cleared" for e in info["events"])


async def test_invalid_fault_is_422():
    async with await client_for(inventory_app()) as c:
        r = await c.post("/admin/fault", json={"mode": "error", "duration_s": 1000})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------ controller proxy
async def test_controller_fault_proxy_allow_list_and_forwarding(make_controller):
    routes = default_routes()
    async with make_controller(routes=routes) as c:
        bad_target = await c.post("/api/faults", json={"target": "gateway", "mode": "error", "duration_s": 5})
        assert bad_target.status_code == 422 and "target must be one of" in bad_target.json()["error"]["message"]
        bad_spec = await c.post("/api/faults", json={"target": "inventory-a", "mode": "error", "duration_s": 999})
        assert bad_spec.status_code == 422 and bad_spec.json()["error"]["details"][0]["loc"] == ["duration_s"]

        ok = await c.post("/api/faults", json={"target": "inventory-a", "mode": "error", "duration_s": 5})
        assert ok.status_code == 200 and ok.json()["active"]["mode"] == "error"
        listing = {f["target"]: f for f in (await c.get("/api/faults")).json()["faults"]}
        assert listing["inventory-a"]["active"]["mode"] == "error" and listing["inventory-b"]["active"] is None
        assert listing.keys() == {"inventory-a", "inventory-b", "pricing"}
        # the service really has the fault
        async with await client_for(routes["inventory-a"]) as svc:
            assert (await svc.post("/v1/stock", json=STOCK)).status_code == 503

        cleared = await c.delete("/api/faults/inventory-a")
        assert cleared.json()["cleared"] is True
        assert (await c.delete("/api/faults/controller")).status_code == 404
        types = [e["type"] for e in (await c.get("/api/events")).json()["events"]]
        assert types[:2] == ["fault_cleared", "fault_injected"]


async def test_controller_fault_proxy_reports_unreachable_target(make_controller):
    async with make_controller(routes={}) as c:
        r = await c.post("/api/faults", json={"target": "pricing", "mode": "error", "duration_s": 5})
    assert r.status_code == 502 and r.json()["error"]["code"] == "target_unreachable"


async def test_controller_link_matrix_api(make_controller):
    async with make_controller() as c:
        v1 = (await c.get("/api/placement")).json()
        assert v1["link_rtt_ms"]["local--aws-simulated"] == 40 and v1["links_source"] == "file"
        # reversed key updates the existing pair
        r = await c.put("/api/links", json={"link_rtt_ms": {"aws-simulated--local": 120}})
        assert r.status_code == 200
        v2 = r.json()
        assert v2["link_rtt_ms"]["local--aws-simulated"] == 120 and v2["version"] == v1["version"] + 1
        assert "aws-simulated--local" not in v2["link_rtt_ms"]
        for bad in ({"link_rtt_ms": {"local--aws-simulated": -1}}, {"link_rtt_ms": {"local--mars-simulated": 5}},
                    {"link_rtt_ms": {}}, {"nope": 1}):
            r = await c.put("/api/links", json=bad)
            assert r.status_code == 422, bad
        assert (await c.get("/api/placement")).json()["version"] == v2["version"]
        reset = (await c.post("/api/placement/reset")).json()
        assert reset["link_rtt_ms"]["local--aws-simulated"] == 40 and reset["links_source"] == "file"
