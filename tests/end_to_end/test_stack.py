"""End-to-end tests against a running Docker Compose stack.

Run with:  docker compose --profile test run --rm tests pytest -m e2e
(the `tests` container sets SETU_E2E_GATEWAY_URL=http://gateway:8000)
Failure-path checks that need to stop or pause containers are run from the host by
scripts/phase2_acceptance.sh, because the test container has no access to the Docker API.
"""
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e

GATEWAY = os.environ.get("SETU_E2E_GATEWAY_URL")


@pytest.fixture(scope="module")
def gw():
    if not GATEWAY:
        pytest.skip("SETU_E2E_GATEWAY_URL not set; stack not available")
    with httpx.Client(base_url=GATEWAY, timeout=10, trust_env=False) as c:
        yield c


def test_gateway_ready(gw):
    r = gw.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_all_dependencies_ready(gw):
    deps = gw.get("/health/dependencies").json()["dependencies"]
    assert {d["name"]: d["status"] for d in deps} == {"inventory": "ready", "pricing": "ready"}


def test_order_confirmed_with_expected_values(gw):
    r = gw.post("/api/orders", json={"sku": "SKU-001", "quantity": 2, "customer_tier": "standard"},
                headers={"X-Request-ID": "e2e-stack-1"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "confirmed" and b["request_id"] == "e2e-stack-1"
    assert b["inventory"]["available"] == 8 and b["pricing"]["total_cents"] == 11000
    assert b["inventory"]["instance"] == "inventory-a"


def test_rejected_and_invalid(gw):
    assert gw.post("/api/orders", json={"sku": "SKU-001", "quantity": 9}).status_code == 409
    r = gw.post("/api/orders", json={"sku": "bad", "quantity": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
