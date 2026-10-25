"""Runner service API, controller proxy and the gateway reset endpoint."""
from __future__ import annotations

import asyncio
import shutil

import httpx
import pytest
import yaml
from fastapi import FastAPI

from setu_x.common.config import ControllerSettings, RunnerSettings
from setu_x.services.controller.app import build_app as build_controller
from setu_x.services.experiments.app import build_app as build_runner
from tests.conftest import CONFIG_DIR, HostRouter, asgi, build_config

TINY = {"id": "tiny", "title": "tiny", "workload": {"rate_rps": 10, "warmup_s": 0, "duration_s": 0.5}, "runs": 1,
        "cooldown_s": 0, "conditions": [{"name": "a"}]}


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, d)
    (d / "scenarios" / "tiny.yaml").write_text(yaml.safe_dump(TINY))
    (d / "scenarios" / "broken.yaml").write_text("id: broken\n")
    return d


async def runner_client(stack, config_dir, tmp_path):
    settings = RunnerSettings(service_name="runner", instance="runner", config_dir=str(config_dir),
                              results_dir=str(tmp_path / "results"), controller_url=stack.controller_url,
                              gateway_url=stack.gateway_url)
    app = build_runner(settings, client=stack.client, load_client=stack.client, config=build_config())
    return app


async def wait_done(client, exp_id, timeout=20):
    for _ in range(int(timeout / 0.1)):
        body = (await client.get(f"/api/experiments/{exp_id}")).json()
        if body["status"] in ("completed", "failed", "stopped"):
            return body
        await asyncio.sleep(0.1)
    raise AssertionError("experiment did not finish")


async def test_runner_api_lifecycle(make_stack, config_dir, tmp_path):
    async with make_stack() as stack:
        app = await runner_client(stack, config_dir, tmp_path)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app),
                                                                       base_url="http://runner") as c:
            sc = {s["name"]: s for s in (await c.get("/api/scenarios")).json()["scenarios"]}
            assert sc["tiny"]["valid"] and sc["A_baseline"]["valid"] and not sc["broken"]["valid"]
            assert (await c.post("/api/experiments", json={"scenario": "nope"})).status_code == 404
            assert (await c.post("/api/experiments", json={"scenario": "../etc"})).status_code == 422
            assert (await c.post("/api/experiments", json={"scenario": "broken"})).status_code == 422
            r = await c.post("/api/experiments", json={"scenario": "smoke_simulated"})
            assert r.status_code == 409 and r.json()["error"]["code"] == "preflight_failed"

            r = await c.post("/api/experiments", json={"scenario": "tiny", "git_commit": "abcdef1", "label": "t1"})
            assert r.status_code == 202, r.text
            exp = r.json()["experiment_id"]
            again = await c.post("/api/experiments", json={"scenario": "tiny"})
            assert again.status_code == 409 and again.json()["error"]["code"] == "experiment_running"
            done = await wait_done(c, exp)
            assert done["status"] == "completed" and done["validation"]["ok"]
            listed = (await c.get("/api/experiments")).json()["experiments"]
            assert listed[0]["experiment_id"] == exp
            f = await c.get(f"/api/experiments/{exp}/files/observations.csv")
            assert f.status_code == 200 and f.text.startswith("experiment_id,condition,")
            assert (await c.get(f"/api/experiments/{exp}/files/secret.txt")).status_code == 404
            assert (await c.get("/api/experiments/nope/files/summary.json")).status_code == 404
            assert (await c.post(f"/api/experiments/{exp}/stop")).status_code == 409


async def test_runner_stop_endpoint(make_stack, config_dir, tmp_path):
    long = {**TINY, "id": "long", "runs": 5, "workload": {"rate_rps": 5, "warmup_s": 0, "duration_s": 1}}
    (config_dir / "scenarios" / "long.yaml").write_text(yaml.safe_dump(long))
    async with make_stack() as stack:
        app = await runner_client(stack, config_dir, tmp_path)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app),
                                                                       base_url="http://runner") as c:
            exp = (await c.post("/api/experiments", json={"scenario": "long"})).json()["experiment_id"]
            await asyncio.sleep(1.5)
            r = await c.post(f"/api/experiments/{exp}/stop")
            assert r.status_code == 200 and r.json()["status"] == "stopped"


def fake_runner() -> FastAPI:
    app = FastAPI()

    @app.get("/api/scenarios")
    async def s():
        return {"scenarios": [{"name": "x"}]}

    @app.post("/api/experiments")
    async def start(body: dict):
        return {"echo": body}

    @app.get("/api/experiments/{exp}/files/{name}")
    async def f(exp: str, name: str):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("a,b\n1,2\n" * 1000, media_type="text/csv")

    return app


async def controller_with(routes, runner_url="http://runner"):
    router = HostRouter({h: asgi(a) for h, a in routes.items()})
    http = httpx.AsyncClient(transport=router)
    app = build_controller(ControllerSettings(service_name="controller", instance="controller",
                                              config_dir=str(CONFIG_DIR), runner_url=runner_url),
                           client=http, config=build_config(), environ={})
    return app, http


async def test_controller_proxies_runner():
    app, http = await controller_with({"runner": fake_runner()})
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app),
                                                                   base_url="http://controller") as c:
        assert (await c.get("/api/scenarios")).json() == {"scenarios": [{"name": "x"}]}
        assert (await c.post("/api/experiments", json={"scenario": "x"})).json() == {"echo": {"scenario": "x"}}
        f = await c.get("/api/experiments/abc/files/observations.csv")
        assert f.status_code == 200 and f.text.count("\n") == 2000 and f.headers["content-type"].startswith("text/csv")
        assert (await c.get("/api/experiments/abc/files/..%2Fsecret")).status_code == 404
        assert (await c.get("/api/experiments/abc/files/x.exe")).status_code == 404
    await http.aclose()


async def test_controller_runner_unreachable_or_disabled():
    app, http = await controller_with({})
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app),
                                                                   base_url="http://controller") as c:
        r = await c.get("/api/experiments")
        assert r.status_code == 503 and r.json()["error"]["code"] == "runner_unreachable"
    await http.aclose()
    app, http = await controller_with({}, runner_url="")
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app),
                                                                   base_url="http://controller") as c:
        r = await c.get("/api/scenarios")
        assert r.status_code == 503 and r.json()["error"]["code"] == "runner_disabled"
    await http.aclose()


async def test_gateway_reset_endpoint_syncs_and_reseeds(make_stack):
    async with make_stack() as stack:
        c = stack.client
        changed = build_config(retries=2, fallback=False)[1].model_dump()
        assert (await c.put("http://controller/api/placement", json=changed)).status_code == 200
        r = (await c.post("http://gateway/api/resilience/reset", json={"seed": 42})).json()
        snap = (await c.get("http://controller/api/placement")).json()
        assert r["synced"] is True and r["seed"] == 42
        assert r["placement_version"] == snap["version"] and r["placement_sha256"] == snap["sha256"]

        async def backoffs_after_reset() -> list[float]:
            reset = (await c.post("http://gateway/api/resilience/reset", json={"seed": 42})).json()
            await c.post("http://controller/api/faults",
                         json={"target": "inventory-a", "mode": "error", "duration_s": 5})
            for _ in range(3):
                await c.post("http://gateway/api/orders", json={"sku": "SKU-002", "quantity": 1})
            await c.delete("http://controller/api/faults")
            ev = (await c.get("http://gateway/api/resilience/events",
                              params={"type": "retry", "since": reset["events_last_seq"]})).json()["events"]
            return [e["backoff_ms"] for e in reversed(ev)]

        first, second = await backoffs_after_reset(), await backoffs_after_reset()
        assert len(first) == 6 and first == second  # same seed -> identical jitter sequence

        events = (await c.get("http://gateway/api/resilience/events?type=resilience_reset")).json()
        assert events["events"][0]["seed"] == 42
        since = (await c.get(f"http://gateway/api/resilience/events?since={events['last_seq']}")).json()
        assert since["events"] == []
        assert (await c.post("http://gateway/api/resilience/reset", json={"seed": -1})).status_code == 422
