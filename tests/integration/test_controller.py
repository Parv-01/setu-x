"""Controller API in-process: environments, adapters, placement GET/PUT/reset, services health."""
import copy

from tests.conftest import default_routes, inventory_app, pricing_app


async def test_environments_mark_simulated_labels(make_controller):
    async with make_controller() as c:
        body = (await c.get("/api/environments")).json()
    envs = {e["name"]: e for e in body["environments"]}
    assert envs["local"]["simulated"] is False and envs["local"]["real_provider_adapter"] is None
    for name in ("aws-simulated", "azure-simulated", "gcp-simulated"):
        e = envs[name]
        assert e["simulated"] is True and "simulation label" in e["label_notice"]
        assert e["real_provider_adapter"]["status"] == "not_configured"
    assert "not measured" in body["link_rtt_note"] and body["mode"] == "local"


async def test_adapters_endpoint_reports_honest_statuses(make_controller):
    async with make_controller() as c:
        body = (await c.get("/api/adapters")).json()
    st = {a["name"]: a["status"] for a in body["adapters"]}
    assert st == {"local-docker": "local", "aws": "not_configured", "azure": "not_configured", "gcp": "not_configured"}
    local = next(a for a in body["adapters"] if a["name"] == "local-docker")
    assert local["capabilities"]["health_check"] == "implemented"
    assert local["capabilities"]["deploy_service"] == "not_implemented"
    assert local["capabilities"]["estimate_cost"] == "unsupported_by_provider"
    for a in body["adapters"]:
        assert a["status"] not in {"connected", "deployed", "verified"}


async def test_adapter_config_error_is_reported_not_fatal(make_controller):
    async with make_controller(environ={"SETU_AWS_REGION": "ap-south-1"}) as c:
        body = (await c.get("/api/adapters")).json()
        envs = (await c.get("/api/environments")).json()["environments"]
    assert "SETU_AWS_EXECUTION_ROLE_ARN" in body["config_errors"]["aws"]
    aws_env = next(e for e in envs if e["name"] == "aws-simulated")
    assert aws_env["real_provider_adapter"]["status"] == "config_error"


async def test_placement_get_put_reset_versioning(make_controller):
    async with make_controller() as c:
        first = (await c.get("/api/placement")).json()
        assert first["version"] == 1 and first["source"] == "file"
        p = copy.deepcopy(first["placement"])
        p["services"]["inventory"]["routing"] = "round-robin"
        p["services"]["inventory"]["instances"][1]["env"] = "local"  # relabel is allowed
        r = await c.put("/api/placement", json=p)
        assert r.status_code == 200, r.text
        second = r.json()
        assert second["version"] == 2 and second["source"] == "api" and second["sha256"] != first["sha256"]
        assert (await c.get("/api/placement")).json()["placement"]["services"]["inventory"]["routing"] == "round-robin"
        reset = (await c.post("/api/placement/reset")).json()
        assert reset["version"] == 3 and reset["source"] == "file" and reset["sha256"] == first["sha256"]


async def test_invalid_put_is_422_names_field_and_keeps_current(make_controller):
    async with make_controller() as c:
        current = (await c.get("/api/placement")).json()
        p = copy.deepcopy(current["placement"])
        p["services"]["inventory"]["instances"][0]["env"] = "mars-simulated"
        r = await c.put("/api/placement", json=p)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "invalid_placement"
        assert "services.inventory.instances.0.env: unknown environment 'mars-simulated'" in err["message"]
        assert (await c.get("/api/placement")).json()["version"] == current["version"]
        bad_json = await c.put("/api/placement", content=b"{nope", headers={"content-type": "application/json"})
        assert bad_json.status_code == 422


async def test_put_cannot_introduce_new_urls(make_controller):
    async with make_controller() as c:
        p = (await c.get("/api/placement")).json()["placement"]
        p["services"]["pricing"]["instances"][0]["url"] = "http://evil.example:8000"
        r = await c.put("/api/placement", json=p)
    assert r.status_code == 422 and "must match configs/placement.yaml" in r.json()["error"]["message"]


async def test_services_health_aggregates_all_instances(make_controller):
    routes = {**default_routes()}
    routes.pop("inventory-b")  # simulate a stopped container
    async with make_controller(routes=routes) as c:
        rows = (await c.get("/api/services")).json()["services"]
    st = {r["instance"]: (r["status"], r["simulated"]) for r in rows}
    assert st["inventory-a"] == ("ready", True) and st["pricing"] == ("ready", True)
    assert st["inventory-b"] == ("unreachable", True)
    assert st["gateway"][0] == "unreachable"  # gateway not routed in this test


async def test_controller_health(make_controller):
    async with make_controller(routes={"inventory-a": inventory_app(), "pricing": pricing_app()}) as c:
        ready = (await c.get("/health/ready")).json()
    assert ready["status"] == "ready" and ready["checks"] == {"config": "ok", "adapters": "ok"}
