"""Open-loop generator behaviour against in-process HTTP apps (real time, small rates)."""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI

from setu_x.experiments.loadgen import generate, plan_requests
from setu_x.experiments.schedule import arrival_offsets
from tests.conftest import HostRouter, asgi

BASE = {"experiment_id": "e", "condition": "c", "run_index": 0, "run_id": "c-r0"}


def slow_gateway(delay_s: float) -> FastAPI:
    app = FastAPI()

    @app.post("/api/orders")
    async def order():
        await asyncio.sleep(delay_s)
        return {"status": "confirmed", "timings_ms": {"total": delay_s * 1000}, "placement": {"version": 1}}

    return app


def plan(rate=20, total=1.0, warmup=0.0):
    return plan_requests(arrival_offsets(rate, total, "constant", 0), warmup, "t", lambda i: {"sku": "SKU-002"})


async def run(app: FastAPI | None, p, max_in_flight=64, timeout_s=5.0):
    routes = {"gateway": asgi(app)} if app else {}
    async with httpx.AsyncClient(transport=HostRouter(routes)) as client:
        start = time.perf_counter() + 0.05
        rows = await generate(client, "http://gateway/api/orders", p, start=start, max_in_flight=max_in_flight,
                              timeout_s=timeout_s, base_row=BASE)
        return rows, start


async def test_open_loop_sends_on_schedule_even_when_responses_are_slow():
    # 300 ms responses at 20 req/s: a closed-loop client with one connection would send ~3 requests/s
    rows, _ = await run(slow_gateway(0.3), plan(20, 1.0))
    assert len(rows) == 20 and all(r["outcome"] == "confirmed" for r in rows)
    assert max(r["send_lag_ms"] for r in rows) < 50
    assert all(280 <= r["latency_ms"] < 600 for r in rows)
    assert [r["seq"] for r in rows] == list(range(20))
    assert len({r["request_id"] for r in rows}) == 20


async def test_in_flight_cap_records_drops():
    rows, _ = await run(slow_gateway(0.5), plan(20, 0.5), max_in_flight=3)
    dropped = [r for r in rows if r["outcome"] == "dropped"]
    assert len(rows) == 10 and len(dropped) == 7
    assert all(r.get("latency_ms") is None and r.get("http_status") is None for r in dropped)


async def test_client_timeout_and_transport_error():
    rows, _ = await run(slow_gateway(1.0), plan(4, 0.5), timeout_s=0.2)
    assert {r["outcome"] for r in rows} == {"client_timeout"}
    assert all(r["latency_ms"] >= 190 for r in rows)
    rows, _ = await run(None, plan(4, 0.5))
    assert {r["outcome"] for r in rows} == {"client_error"} and rows[0]["error_type"] == "ConnectError"


async def test_latency_measured_from_scheduled_time_includes_lag():
    # a blocking server makes the event loop late; the lag must show up in latency_ms
    app = FastAPI()

    @app.post("/api/orders")
    async def order():
        time.sleep(0.05)  # blocks the shared loop on purpose
        return {"status": "confirmed"}

    rows, _ = await run(app, plan(100, 0.3))
    assert any(r["send_lag_ms"] > 5 for r in rows)
    for r in rows:
        assert r["latency_ms"] + 1e-6 >= r["service_latency_ms"]
        assert abs(r["latency_ms"] - r["service_latency_ms"] - r["send_lag_ms"]) < 2.0


async def test_phases_from_warmup():
    rows, _ = await run(slow_gateway(0.0), plan(10, 1.0, warmup=0.5))
    assert [r["phase"] for r in rows] == ["warmup"] * 5 + ["measure"] * 5
    assert rows[5]["measure_offset_s"] == 0.0 and rows[0]["measure_offset_s"] == -0.5


async def test_scenario_timeout_overrides_the_client_default():
    # regression (Phase 8 pilot): the runner's httpx client has a 5 s default timeout, which cut
    # requests that the scenario allowed 15 s. The scenario's request timeout must be the one sent.
    router = HostRouter({"gateway": asgi(slow_gateway(0.0))})
    async with httpx.AsyncClient(transport=router, timeout=0.2) as client:
        await generate(client, "http://gateway/api/orders", plan(4, 0.5), start=time.perf_counter() + 0.05,
                       max_in_flight=8, timeout_s=15.0, base_row=BASE)
    assert router.requests
    for req in router.requests:
        t = req.extensions["timeout"]
        assert t["read"] == 15.0 and t["write"] == 15.0 and t["pool"] == 15.0 and t["connect"] == 5.0
