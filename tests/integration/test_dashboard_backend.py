"""Backend pieces the dashboard relies on (Phase 6): static serving with CSP, browser protections,
gateway views, bounded experiment overrides and the live endpoint."""
from __future__ import annotations

import asyncio
import json
import shutil

import httpx
import pytest
import yaml

from setu_x.common.config import RunnerSettings
from setu_x.services.controller.app import _hostname
from setu_x.services.experiments.app import build_app as build_runner
from tests.conftest import CONFIG_DIR, asgi, build_config


async def test_dashboard_is_served_with_security_headers(make_stack):
    async with make_stack() as stack:
        c = stack.client
        r = await c.get("http://controller/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/dashboard/"
        page = await c.get("http://controller/dashboard/")
        assert page.status_code == 200 and "<title>SETU-X Dashboard</title>" in page.text
        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
        assert page.headers["x-content-type-options"] == "nosniff"
        js = await c.get("http://controller/dashboard/js/app.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert "content-security-policy" not in (await c.get("http://controller/api/placement")).headers


async def test_dashboard_has_no_inline_scripts_or_styles():
    # the CSP forbids them; this keeps the static files compatible with it
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "dashboard"
    html = (root / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and " style=" not in html and "onclick=" not in html
    for js in (root / "js").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert "innerHTML" not in text and "setAttribute(\"style\"" not in text, js.name


@pytest.mark.parametrize("host, expected", [("a:80", "a"), ("[::1]:8090", "::1"), ("::1", "::1"), ("x", "x")])
def test_hostname_parsing(host, expected):
    assert _hostname(host) == expected


async def test_foreign_origins_and_hosts_are_refused(make_stack):
    async with make_stack() as stack:
        c = stack.client
        evil = await c.post("http://controller/api/faults", headers={"Origin": "http://evil.example"},
                            json={"target": "inventory-a", "mode": "error", "duration_s": 5})
        assert evil.status_code == 403 and evil.json()["error"]["code"] == "forbidden_origin"
        faults = (await c.get("http://controller/api/faults")).json()["faults"]
        assert all(f["active"] is None for f in faults)
        reset = await c.post("http://controller/api/placement/reset", headers={"Origin": "https://evil.example"})
        assert reset.status_code == 403
        null_origin = await c.post("http://controller/api/placement/reset", headers={"Origin": "null"})
        assert null_origin.status_code == 403
        for origin in ("http://127.0.0.1:8090", "http://localhost:9999", "http://[::1]:8090", "http://controller"):
            ok = await c.post("http://controller/api/placement/reset", headers={"Origin": origin})
            assert ok.status_code == 200, origin
        # GETs from anywhere are fine (read-only; the browser's same-origin policy hides the response)
        read = await c.get("http://controller/api/placement", headers={"Origin": "http://evil.example"})
        assert read.status_code == 200
        rebinding = await c.get("http://controller/api/placement", headers={"Host": "attacker.example"})
        assert rebinding.status_code == 400 and rebinding.json()["error"]["code"] == "invalid_host"
        for host in ("127.0.0.1:8090", "localhost:8090", "[::1]:8090", "controller:8000"):
            assert (await c.get("http://controller/api/placement", headers={"Host": host})).status_code == 200, host


async def test_gateway_views_and_test_order(make_stack):
    async with make_stack() as stack:
        c = stack.client
        order = await c.post("http://controller/api/gateway/orders", json={"sku": "SKU-002", "quantity": 1})
        assert order.status_code == 200 and order.json()["status"] == "confirmed"
        bad = await c.post("http://controller/api/gateway/orders", json={"sku": "nope", "quantity": 1})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
        d = (await c.get("http://controller/api/gateway/routing/decisions", params={"limit": 5})).json()
        assert {x["service"] for x in d["decisions"]} == {"inventory", "pricing"}
        for view in ("resilience/events", "resilience/breakers", "placement/current", "dependencies"):
            assert (await c.get(f"http://controller/api/gateway/{view}")).status_code == 200, view
        nope = await c.get("http://controller/api/gateway/admin/fault")
        assert nope.status_code == 404
        del stack.router.routes["gateway"]
        down = await c.get("http://controller/api/gateway/routing/decisions")
        assert down.status_code == 503 and down.json()["error"]["code"] == "gateway_unreachable"


async def test_services_include_runner_when_configured(make_controller):
    async with make_controller() as c:
        names = [s["instance"] for s in (await c.get("/api/services")).json()["services"]]
        assert "runner" in names and "gateway" in names


# ------------------------------------------------------------------------------------------ runner
@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, d)
    tiny = {"id": "tiny", "title": "tiny", "workload": {"rate_rps": 10, "warmup_s": 0, "duration_s": 1.5},
            "runs": 2, "cooldown_s": 0, "defaults": {"resilience": {"fallback": False}},
            "conditions": [{"name": "a"},
                           {"name": "f", "faults": [{"target": "inventory-a", "mode": "error", "at_s": 1,
                                                     "duration_s": 1}]}]}
    (d / "scenarios" / "tiny.yaml").write_text(yaml.safe_dump(tiny))
    return d


def runner_app(stack, config_dir, tmp_path):
    settings = RunnerSettings(service_name="runner", instance="runner", config_dir=str(config_dir),
                              results_dir=str(tmp_path / "results"), controller_url=stack.controller_url,
                              gateway_url=stack.gateway_url)
    return build_runner(settings, client=stack.client, load_client=stack.client, config=build_config())


async def wait_done(c, exp_id, timeout=30):
    for _ in range(int(timeout / 0.1)):
        body = (await c.get(f"/api/experiments/{exp_id}")).json()
        if body["status"] in ("completed", "failed", "stopped"):
            return body
        await asyncio.sleep(0.1)
    raise AssertionError("not finished")


@pytest.mark.parametrize("overrides, message", [
    ({"rate_rps": 500}, "less than or equal to 100"),
    ({"duration_s": 0.5}, "measurement window"),          # the fault at 1 s no longer fits
    ({"links": {"local--aws-simulated": 5}}, "simulated"),  # local-mode scenario
    ({"runs": 0}, "greater than or equal to 1"),
    ({"bogus": 1}, "Extra inputs"),
])
async def test_overrides_are_bounded(make_stack, config_dir, tmp_path, overrides, message):
    async with make_stack() as stack:
        app = runner_app(stack, config_dir, tmp_path)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app), base_url="http://r") as c:
            r = await c.post("/api/experiments", json={"scenario": "tiny", "overrides": overrides})
            assert r.status_code == 422, r.text
            assert message in json.dumps(r.json())


async def test_overrides_applied_recorded_and_live_view(make_stack, config_dir, tmp_path):
    async with make_stack() as stack:
        app = runner_app(stack, config_dir, tmp_path)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=asgi(app), base_url="http://r") as c:
            r = await c.post("/api/experiments", json={"scenario": "tiny",
                                                       "overrides": {"runs": 1, "duration_s": 2.5, "rate_rps": 8}})
            assert r.status_code == 202, r.text
            exp = r.json()["experiment_id"]
            assert r.json()["progress"]["total_runs"] == 2
            seen = None
            for _ in range(60):
                live = await c.get(f"/api/experiments/{exp}/live")
                assert live.status_code == 200
                body = live.json()
                if body["seconds"]:
                    seen = body
                    break
                await asyncio.sleep(0.1)
            assert seen is not None and "provisional" in seen["label"].lower()
            assert seen["run"]["condition"] in ("a", "f")
            done = await wait_done(c, exp)
            assert done["status"] == "completed" and done["validation"]["ok"]
            final = (await c.get(f"/api/experiments/{exp}/live")).json()
            assert len(final["completed_runs"]) == 2
            fault_run = next(x for x in final["completed_runs"] if x["condition"] == "f")
            assert fault_run["success_rate"] < 1.0
            manifest = (await c.get(f"/api/experiments/{exp}/files/manifest.json")).json()
            assert manifest["overrides"] == {"runs": 1, "duration_s": 2.5, "rate_rps": 8}
            assert manifest["scenario"]["workload"]["rate_rps"] == 8 and manifest["scenario"]["runs"] == 1
            assert "overrides" in manifest["overrides_note"]
            assert all(run["scheduled"] == 20 for run in manifest["runs"])  # 8 req/s x 2.5 s
            assert (await c.get("/api/experiments/nope/live")).status_code == 404
            sc = {s["name"]: s for s in (await c.get("/api/scenarios")).json()["scenarios"]}
            detail = {d["name"]: d for d in sc["tiny"]["condition_detail"]}
            assert detail["f"]["faults"][0]["at_s"] == 1 and sc["tiny"]["comment"] == ""
            assert sc["B_network_delay"]["default_links"]["local--aws-simulated"] == 50
            assert "Experiment B" in sc["B_network_delay"]["comment"]
