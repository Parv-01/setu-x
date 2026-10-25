"""Gateway routing across inventory instances and placement sync with the controller (in-process)."""
import copy
from collections import Counter

from setu_x.common.config import ControllerSettings
from setu_x.services.controller.app import build_app as build_controller
from tests.conftest import CONFIG_DIR, DEFAULT_TEST_RESILIENCE, build_config, default_routes

ORDER = {"sku": "SKU-001", "quantity": 1}


async def orders(client, n):
    out = []
    for _ in range(n):
        r = await client.post("/api/orders", json=ORDER)
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


async def test_round_robin_spreads_orders_and_reports_routing(make_gateway):
    cfg = build_config(inventory={"routing": "round-robin"}, resilience=DEFAULT_TEST_RESILIENCE)
    async with make_gateway(config=cfg) as (client, router):
        bodies = await orders(client, 4)
        decisions = (await client.get("/api/routing/decisions", params={"limit": 10})).json()["decisions"]
    served = [b["inventory"]["instance"] for b in bodies]
    assert served == ["inventory-a", "inventory-b", "inventory-a", "inventory-b"]
    # the gateway really called the chosen instance, and the instance confirms it
    assert [h for h in router.hosts() if h.startswith("inventory")] == served
    assert [b["inventory"]["reported_by"] for b in bodies][1] == "inventory-b@gcp-simulated"
    assert bodies[1]["inventory"]["env"] == "gcp-simulated" and bodies[1]["inventory"]["simulated"] is True
    inv_decisions = [d for d in decisions if d["service"] == "inventory"]
    assert [d["instance"] for d in inv_decisions][:4] == served[::-1]  # newest first
    assert inv_decisions[0]["reason"].startswith("round-robin position")


async def test_weighted_routing_proportions(make_gateway):
    _, placement = build_config()
    insts = placement.model_dump()["services"]["inventory"]["instances"]
    insts[0]["weight"], insts[1]["weight"] = 3, 1
    cfg = build_config(inventory={"routing": "weighted", "instances": insts}, resilience=DEFAULT_TEST_RESILIENCE)
    async with make_gateway(config=cfg) as (client, _):
        bodies = await orders(client, 8)
    assert Counter(b["inventory"]["instance"] for b in bodies) == {"inventory-a": 6, "inventory-b": 2}


async def test_disabled_primary_routes_to_secondary(make_gateway):
    _, placement = build_config()
    insts = placement.model_dump()["services"]["inventory"]["instances"]
    insts[0]["enabled"] = False
    cfg = build_config(inventory={"instances": insts}, resilience=DEFAULT_TEST_RESILIENCE)
    async with make_gateway(config=cfg) as (client, router):
        body = (await orders(client, 1))[0]
    assert body["inventory"]["instance"] == "inventory-b"
    assert "primary 'inventory-a' disabled" in body["routing"][0]["reason"]
    assert "inventory-a" not in router.hosts()


async def test_gateway_follows_controller_placement_and_survives_controller_loss(make_gateway):
    controller = build_controller(
        ControllerSettings(service_name="controller", instance="controller", config_dir=str(CONFIG_DIR)),
        config=build_config(), environ={},
    )
    routes = {**default_routes(), "controller": controller}
    async with controller.router.lifespan_context(controller):
        async with make_gateway(routes=routes, controller_url="http://controller:8000") as (client, router):
            sync = client.app.state.placement_sync
            assert await sync.sync_once() is True
            assert sync.source == "controller" and sync.version == 1

            # change routing through the controller API
            store = controller.state.store
            p = copy.deepcopy(store.snapshot()["placement"])
            p["services"]["inventory"]["routing"] = "round-robin"
            store.replace(p)
            assert await sync.sync_once() is True
            assert sync.version == 2
            served = [b["inventory"]["instance"] for b in await orders(client, 2)]
            assert served == ["inventory-a", "inventory-b"]
            assert (await client.get("/api/placement/current")).json()["placement_version"] == 2

            # controller disappears: gateway keeps the last valid placement and keeps serving
            router.routes.pop("controller")
            assert await sync.sync_once() is False
            info = (await client.get("/health/ready")).json()["info"]
            assert info["controller_reachable"] is False and info["placement_source"] == "controller"
            assert info["placement_version"] == 2 and "ConnectError" in info["last_error"]
            served = [b["inventory"]["instance"] for b in await orders(client, 2)]
            assert served == ["inventory-a", "inventory-b"]


async def test_gateway_ignores_invalid_controller_placement(make_gateway):
    from fastapi import FastAPI

    fake = FastAPI()

    @fake.get("/api/placement")
    async def bad():
        return {"version": 9, "placement": {"call_mode": "parallel", "services": {}}}

    routes = {**default_routes(), "controller": fake}
    async with make_gateway(routes=routes, controller_url="http://controller:8000") as (client, _):
        sync = client.app.state.placement_sync
        before = sync.placement.fingerprint()
        assert await sync.sync_once() is False
        assert sync.placement.fingerprint() == before and sync.source == "file"
        assert "services.inventory" in sync.last_error and sync.controller_reachable is True
        assert (await client.post("/api/orders", json=ORDER)).status_code == 200


async def test_sequential_out_of_stock_does_not_log_pricing_decision(make_gateway):
    async with make_gateway(call_mode="sequential") as (client, _):
        r = await client.post("/api/orders", json={"sku": "SKU-001", "quantity": 9})
        decisions = (await client.get("/api/routing/decisions")).json()["decisions"]
    assert r.status_code == 409
    assert [d["service"] for d in decisions] == ["inventory"]
