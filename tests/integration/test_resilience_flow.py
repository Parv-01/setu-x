"""Gateway resilience end to end, in-process: fallback, retries, breaker, deadlines, link delay."""
import asyncio
import time

from setu_x.common.config import ControllerSettings
from setu_x.services.controller.app import build_app as build_controller
from tests.conftest import (
    CONFIG_DIR,
    DEFAULT_TEST_RESILIENCE,
    Scripted,
    build_config,
    default_routes,
    hanging_app,
    inventory_app,
    pricing_app,
)

ORDER = {"sku": "SKU-001", "quantity": 1}
FAST = {"timeout_ms": 300, "connect_timeout_ms": 100, "outer_deadline_ms": 2000}


def routes_with(inv_a=None, inv_b=None, pricing=None):
    r = default_routes()
    for name, app in (("inventory-a", inv_a), ("inventory-b", inv_b), ("pricing", pricing)):
        if app is not None:
            r[name] = app
    return r


async def test_fallback_to_secondary_when_primary_fails(make_gateway):
    a = Scripted(lambda n: 500)
    async with make_gateway(routes=routes_with(inv_a=a.app), **FAST) as (client, _):
        r = await client.post("/api/orders", json=ORDER)
        events = (await client.get("/api/resilience/events", params={"type": "fallback"})).json()["events"]
    body = r.json()
    assert r.status_code == 200 and body["status"] == "confirmed"
    assert body["inventory"]["instance"] == "inventory-b" and body["inventory"]["env"] == "gcp-simulated"
    inv = body["dependencies"][0]
    assert inv["fallback_used"] is True and inv["tried"] == [
        {"instance": "inventory-a", "outcome": "http_5xx", "attempts": 1},
        {"instance": "inventory-b", "outcome": "ok", "attempts": 1}]
    assert body["routing"][0]["instance"] == "inventory-a" and body["routing"][0]["served_instance"] == "inventory-b"
    assert body["resilience"]["fallbacks"] == 1
    assert events[0]["from_instance"] == "inventory-a" and events[0]["to_instance"] == "inventory-b"


async def test_no_fallback_when_disabled(make_gateway):
    a = Scripted(lambda n: 500)
    async with make_gateway(routes=routes_with(inv_a=a.app), fallback=False, **FAST) as (client, router):
        r = await client.post("/api/orders", json=ORDER)
    assert r.status_code == 503 and "inventory-b" not in router.hosts()


async def test_retry_amplification_is_bounded(make_gateway):
    a, b = Scripted(lambda n: 503), Scripted(lambda n: 503, instance="inventory-b", env="gcp-simulated")
    async with make_gateway(routes=routes_with(inv_a=a.app, inv_b=b.app), retries=2, **FAST) as (client, _):
        r = await client.post("/api/orders", json=ORDER)
    details = r.json()["error"]["details"]
    inv = details["dependencies"][0]
    assert r.status_code == 503
    assert a.calls == 3 and b.calls == 3  # (1 + retries) per candidate, never more
    assert inv["attempts"] == 6 and inv["retries"] == 4
    assert details["resilience"]["attempts"] == 7  # plus the single successful pricing call


async def test_retry_recovers_transient_failure(make_gateway):
    a = Scripted(lambda n: 503 if n == 1 else None)
    async with make_gateway(routes=routes_with(inv_a=a.app), retries=1, **FAST) as (client, _):
        r = await client.post("/api/orders", json=ORDER)
        retry_events = (await client.get("/api/resilience/events", params={"type": "retry"})).json()["events"]
    body = r.json()
    assert r.status_code == 200 and body["inventory"]["instance"] == "inventory-a" and a.calls == 2
    assert body["resilience"]["retries"] == 1 and body["resilience"]["fallbacks"] == 0
    assert retry_events[0]["previous_error"] == "http_5xx" and 0 <= retry_events[0]["backoff_ms"] <= 50


async def test_no_retry_and_no_fallback_on_4xx(make_gateway):
    a = Scripted(lambda n: 404)
    async with make_gateway(routes=routes_with(inv_a=a.app), retries=3, **FAST) as (client, router):
        r = await client.post("/api/orders", json=ORDER)
    assert r.status_code == 503 and a.calls == 1 and "inventory-b" not in router.hosts()


async def test_circuit_breaker_opens_fails_fast_and_recovers(make_gateway):
    healthy = {"a": False}
    a = Scripted(lambda n: None if healthy["a"] else 500)
    cb = {"enabled": True, "consecutive_failures": 3, "reset_timeout_ms": 300}
    async with make_gateway(routes=routes_with(inv_a=a.app), circuit_breaker=cb, **FAST) as (client, _):
        for _ in range(3):  # 3 failures on a (each order falls back to b) -> breaker opens
            assert (await client.post("/api/orders", json=ORDER)).json()["inventory"]["instance"] == "inventory-b"
        assert a.calls == 3
        r = await client.post("/api/orders", json=ORDER)  # open: a is not called at all
        body = r.json()
        assert a.calls == 3 and body["inventory"]["instance"] == "inventory-b"
        assert body["dependencies"][0]["tried"][0] == {"instance": "inventory-a", "outcome": "breaker_open",
                                                       "attempts": 0}
        assert body["resilience"]["breaker_rejections"] == 1
        states = {b["instance"]: b["state"] for b in (await client.get("/api/resilience/breakers")).json()["breakers"]}
        assert states["inventory-a"] == "open" and states["inventory-b"] == "closed"

        healthy["a"] = True
        await asyncio.sleep(0.35)  # past reset_timeout_ms -> next call is the half-open probe
        r = await client.post("/api/orders", json=ORDER)
        assert r.json()["inventory"]["instance"] == "inventory-a" and a.calls == 4
        transitions = (await client.get("/api/resilience/events", params={"type": "breaker_transition"})).json()
    seq = [(e["from_state"], e["to_state"]) for e in transitions["events"] if e["instance"] == "inventory-a"][::-1]
    assert seq == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


async def test_failed_probe_reopens_breaker(make_gateway):
    a = Scripted(lambda n: 500)
    cb = {"enabled": True, "consecutive_failures": 1, "reset_timeout_ms": 200}
    async with make_gateway(routes=routes_with(inv_a=a.app), circuit_breaker=cb, **FAST) as (client, _):
        await client.post("/api/orders", json=ORDER)
        await asyncio.sleep(0.25)
        await client.post("/api/orders", json=ORDER)  # probe fails
        states = {b["instance"]: b["state"] for b in (await client.get("/api/resilience/breakers")).json()["breakers"]}
    assert a.calls == 2 and states["inventory-a"] == "open"


async def test_requests_terminate_within_deadline_even_with_retries(make_gateway):
    routes = routes_with(inv_a=hanging_app(30), inv_b=hanging_app(30))
    async with make_gateway(routes=routes, timeout_ms=200, connect_timeout_ms=100, outer_deadline_ms=500,
                            retries=3) as (client, _):
        t0 = time.perf_counter()
        r = await client.post("/api/orders", json=ORDER)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 503 and elapsed < 0.5 + 0.25, elapsed


async def test_link_delay_injected_only_in_simulated_mode(make_gateway):
    async with make_gateway(mode="local", **FAST) as (client, _):
        local = (await client.post("/api/orders", json=ORDER)).json()
    async with make_gateway(mode="simulated", **FAST) as (client, _):
        t0 = time.perf_counter()
        sim = (await client.post("/api/orders", json=ORDER)).json()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        info = (await client.get("/health/ready")).json()["info"]
    assert local["resilience"]["injected_delay_ms_total"] == 0
    deps = {d["name"]: d for d in sim["dependencies"]}
    assert deps["inventory"]["injected_delay_ms"] == 40 and deps["pricing"]["injected_delay_ms"] == 55
    assert sim["resilience"]["injected_delay_ms_critical_path"] == 55  # parallel: max, not sum
    assert elapsed_ms >= 55 and sim["timings_ms"]["total"] >= 55
    assert sim["environment_mode"] == "simulated" and info["link_delay_injection"] == "on"


async def test_link_delay_sums_in_sequential_mode(make_gateway):
    async with make_gateway(mode="simulated", call_mode="sequential", **FAST) as (client, _):
        body = (await client.post("/api/orders", json=ORDER)).json()
    assert body["resilience"]["injected_delay_ms_critical_path"] == 95 and body["timings_ms"]["total"] >= 95


async def test_link_delay_counts_against_timeout(make_gateway):
    envs, placement = build_config(resilience={**FAST, "fallback": False})
    slow_links = {k: (900 if "aws" in k else v) for k, v in envs.link_rtt_ms.items()}
    envs = envs.model_copy(update={"link_rtt_ms": slow_links})
    async with make_gateway(mode="simulated", config=(envs, placement)) as (client, router):
        r = await client.post("/api/orders", json=ORDER)
    assert r.status_code == 503 and "timeout" in r.json()["error"]["message"]
    # the request never left the gateway: half of 900 ms > 300 ms timeout
    assert "inventory-a" not in router.hosts()


async def test_slow_dependency_cut_by_timeout_then_fallback(make_gateway):
    routes = routes_with(inv_a=hanging_app(30))
    async with make_gateway(routes=routes, **FAST) as (client, _):
        t0 = time.perf_counter()
        body = (await client.post("/api/orders", json=ORDER)).json()
        elapsed = time.perf_counter() - t0
    assert body["inventory"]["instance"] == "inventory-b" and 0.3 <= elapsed < 0.6
    assert body["dependencies"][0]["tried"][0]["outcome"] == "timeout"


async def test_gateway_applies_controller_links_and_resets_breakers(make_gateway):
    shared = build_config(resilience={**DEFAULT_TEST_RESILIENCE, "circuit_breaker": {"enabled": True}})
    controller = build_controller(
        ControllerSettings(service_name="controller", instance="controller", config_dir=str(CONFIG_DIR)),
        config=shared, environ={})
    routes = {**default_routes(), "controller": controller, "inventory-a": inventory_app(),
              "pricing": pricing_app()}
    async with controller.router.lifespan_context(controller):
        async with make_gateway(routes=routes, controller_url="http://controller:8000", mode="simulated",
                                config=shared) as (client, _):
            sync = client.app.state.placement_sync
            await sync.sync_once()
            await client.post("/api/orders", json=ORDER)  # creates breakers
            assert (await client.get("/api/resilience/breakers")).json()["breakers"]
            controller.state.store.update_links({"local--aws-simulated": 10})
            await sync.sync_once()
            body = (await client.post("/api/orders", json=ORDER)).json()
            resets = (await client.get("/api/resilience/events", params={"type": "breakers_reset"})).json()
    assert sync.envs.rtt_ms("local", "aws-simulated") == 10
    assert body["dependencies"][0]["injected_delay_ms"] == 10
    assert resets["events"] and "changed" in resets["events"][0]["reason"]


async def test_retry_skipped_when_backoff_would_overrun_deadline(make_gateway):
    # seed 2 -> first backoff draw = uniform(0, 1000) = 956 ms, far beyond the ~300 ms left
    a = Scripted(lambda n: 503)
    async with make_gateway(routes=routes_with(inv_a=a.app), seed=2, retries=1, fallback=False,
                            backoff={"base_ms": 1000, "max_ms": 1000},
                            timeout_ms=250, connect_timeout_ms=100, outer_deadline_ms=300) as (client, _):
        t0 = time.perf_counter()
        r = await client.post("/api/orders", json=ORDER)
        elapsed = time.perf_counter() - t0
        skipped = (await client.get("/api/resilience/events", params={"type": "retry_skipped"})).json()["events"]
    assert r.status_code == 503 and a.calls == 1
    assert skipped and skipped[0]["reason"] == "outer deadline too close for backoff"
    assert elapsed < 0.3 + 0.1, elapsed  # never sleeps past the outer deadline
