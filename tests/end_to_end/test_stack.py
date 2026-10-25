"""End-to-end tests against a running Docker Compose stack.

Run with:  docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e
(the `tests` container sets SETU_E2E_GATEWAY_URL and SETU_E2E_CONTROLLER_URL)
Failure-path checks that stop or pause containers run from the host (scripts/phase*_acceptance.*),
because the test container has no access to the Docker API.
"""
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.e2e

GATEWAY = os.environ.get("SETU_E2E_GATEWAY_URL")
CONTROLLER = os.environ.get("SETU_E2E_CONTROLLER_URL")


@pytest.fixture(scope="module")
def gw():
    if not GATEWAY:
        pytest.skip("SETU_E2E_GATEWAY_URL not set; stack not available")
    with httpx.Client(base_url=GATEWAY, timeout=10, trust_env=False) as c:
        yield c


@pytest.fixture(scope="module")
def ctl():
    if not CONTROLLER:
        pytest.skip("SETU_E2E_CONTROLLER_URL not set; stack not available")
    with httpx.Client(base_url=CONTROLLER, timeout=10, trust_env=False) as c:
        yield c


def test_gateway_ready(gw):
    r = gw.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_all_dependencies_ready(gw):
    deps = gw.get("/health/dependencies").json()["dependencies"]
    assert {d["instance"]: d["status"] for d in deps} == {
        "inventory-a": "ready", "inventory-b": "ready", "pricing": "ready"}


def test_order_confirmed_with_expected_values(gw):
    r = gw.post("/api/orders", json={"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"},
                headers={"X-Request-ID": "e2e-stack-1"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "confirmed" and b["request_id"] == "e2e-stack-1"
    assert b["inventory"]["available"] == 8 and b["pricing"]["total_cents"] == 11000
    assert b["inventory"]["instance"] == "inventory-a" and b["inventory"]["simulated"] is True


def test_rejected_and_invalid(gw):
    assert gw.post("/api/orders", json={"sku": "SKU-001", "quantity": 9}).status_code == 409
    r = gw.post("/api/orders", json={"sku": "bad", "quantity": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_controller_reports_simulated_envs_and_honest_adapters(ctl):
    envs = {e["name"]: e["simulated"] for e in ctl.get("/api/environments").json()["environments"]}
    assert envs == {"local": False, "aws-simulated": True, "azure-simulated": True, "gcp-simulated": True}
    st = {a["name"]: a["status"] for a in ctl.get("/api/adapters").json()["adapters"]}
    assert st == {"local-docker": "local", "aws": "not_configured", "azure": "not_configured", "gcp": "not_configured"}


def test_controller_sees_all_services_ready(ctl):
    rows = ctl.get("/api/services").json()["services"]
    assert {r["instance"]: r["status"] for r in rows} == {
        "gateway": "ready", "inventory-a": "ready", "inventory-b": "ready", "pricing": "ready"}


def _wait_for_version(gw, version, timeout_s=10):
    end = time.time() + timeout_s
    while time.time() < end:
        if gw.get("/api/placement/current").json()["placement_version"] == version:
            return True
        time.sleep(0.25)
    return False


def test_round_robin_via_controller_then_reset(gw, ctl):
    p = ctl.get("/api/placement").json()["placement"]
    p["services"]["inventory"]["routing"] = "round-robin"
    new = ctl.put("/api/placement", json=p).json()
    try:
        assert _wait_for_version(gw, new["version"]), "gateway did not pick up the new placement"
        served = [gw.post("/api/orders", json={"sku": "SKU-002", "quantity": 1}).json()["inventory"]["instance"]
                  for _ in range(4)]
        assert sorted(served) == ["inventory-a", "inventory-a", "inventory-b", "inventory-b"]
        assert served[0] != served[1]
    finally:
        reset = ctl.post("/api/placement/reset").json()
        assert _wait_for_version(gw, reset["version"])


def test_fault_via_controller_triggers_fallback_then_clears(gw, ctl):
    r = ctl.post("/api/faults", json={"target": "inventory-a", "mode": "error", "duration_s": 30})
    assert r.status_code == 200
    try:
        body = gw.post("/api/orders", json={"sku": "SKU-002", "quantity": 1}).json()
        assert body["status"] == "confirmed" and body["inventory"]["instance"] == "inventory-b"
        assert body["dependencies"][0]["fallback_used"] is True
    finally:
        assert ctl.delete("/api/faults/inventory-a").json()["cleared"] is True
    body = gw.post("/api/orders", json={"sku": "SKU-002", "quantity": 1}).json()
    assert body["inventory"]["instance"] == "inventory-a" and body["dependencies"][0]["fallback_used"] is False


def test_runner_reachable_through_controller(ctl):
    r = ctl.get("/api/scenarios")
    assert r.status_code == 200, r.text
    names = {s["name"]: s for s in r.json()["scenarios"]}
    assert names["smoke"]["valid"] and names["A_baseline"]["valid"]


def test_smoke_experiment_through_controller(ctl):
    mode = ctl.get("/api/environments").json()["mode"]
    scenario = "smoke" if mode == "local" else "smoke_simulated"
    r = ctl.post("/api/experiments", json={"scenario": scenario, "label": "e2e"})
    assert r.status_code == 202, r.text
    exp = r.json()["experiment_id"]
    deadline = time.monotonic() + 120
    body = {}
    while time.monotonic() < deadline:
        body = ctl.get(f"/api/experiments/{exp}").json()
        if body["status"] in ("completed", "failed", "stopped"):
            break
        time.sleep(1)
    assert body["status"] == "completed", body
    assert body["validation"]["ok"], body["validation"]
    csv_text = ctl.get(f"/api/experiments/{exp}/files/observations.csv").text
    assert csv_text.count("\n") == body["validation"]["rows_csv"] + 1
