"""Dashboard tests in a real browser (Chromium via Playwright) against the running stack.

Run (stack up):   docker compose --profile browser run --rm browser-tests
Locally:          SETU_DASHBOARD_URL=http://127.0.0.1:8090 python -m pytest tests/browser   (needs playwright)

They check that every control sends the right request to the right endpoint (captured from the
browser's network traffic), that the backend state changes accordingly, that API failures are shown
as readable errors, that empty data renders as an empty state (never example numbers), and that a
reviewer can run one experiment end to end from the dashboard. They change stack state (placement,
faults, experiments) and restore the placement and faults at the end.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, Request, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.browser
BASE = os.environ.get("SETU_DASHBOARD_URL", "").rstrip("/")
if not BASE:
    pytest.skip("SETU_DASHBOARD_URL not set; dashboard not available", allow_module_level=True)


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as c:
        yield c
        c.post("/api/placement/reset")
        c.delete("/api/faults")


@pytest.fixture(scope="module")
def mode(api) -> str:
    return api.get("/api/environments").json()["mode"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.console_errors = []  # type: ignore[attr-defined]
    pg.on("console", lambda m: m.type == "error" and pg.console_errors.append(m.text))  # type: ignore[attr-defined]
    pg.on("pageerror", lambda e: pg.console_errors.append(str(e)))  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def open_tab(page: Page, tab: str) -> None:
    page.goto(f"{BASE}/dashboard/#{tab}")
    expect(page.locator(f"#tab-{tab}")).to_be_visible()


def body_of(req: Request) -> dict:
    return json.loads(req.post_data or "{}")


# ------------------------------------------------------------------------------------------ loading
def test_dashboard_loads_with_csp_and_no_script_errors(page, api):
    r = api.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/dashboard/"
    resp = page.goto(f"{BASE}/dashboard/")
    csp = resp.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # gateway, runner, inventory-a, inventory-b, pricing
    expect(page.locator("#health tbody tr")).to_have_count(5, timeout=15000)
    expect(page.locator("#health")).not_to_contain_text("unreachable")
    expect(page.locator("#conn")).to_contain_text("controller reachable")
    assert page.console_errors == []


def test_overview_graph_shows_labels_and_configured_delays(page, mode):
    open_tab(page, "overview")
    graph = page.locator("#graph svg")
    expect(graph).to_contain_text("inventory-a · inventory")
    expect(graph).to_contain_text("aws-simulated (label only)")
    if mode == "simulated":
        expect(graph).to_contain_text("ms configured")
    else:
        expect(graph).to_contain_text("no injected delay (local mode)")
    expect(page.locator("#mode-badge")).to_have_text(f"mode: {mode.upper()}")
    expect(page.locator("#sim-notice")).to_contain_text("not deployments")
    expect(page.locator("#adapters")).to_contain_text("not_configured")


# ------------------------------------------------------------------------------------------ controls
def test_test_order_calls_gateway_through_controller(page):
    open_tab(page, "control")
    page.select_option("#order-form select[name=sku]", "SKU-002")
    with page.expect_request(lambda r: r.url.endswith("/api/gateway/orders") and r.method == "POST") as req:
        page.click("#order-send")
    assert body_of(req.value) == {"sku": "SKU-002", "quantity": 1, "customer_tier": "standard"}
    expect(page.locator("#order-result")).to_contain_text("CONFIRMED")
    expect(page.locator("#order-result")).to_contain_text("(configured)")
    page.click("#gw-refresh")
    expect(page.locator("#decisions")).to_contain_text("inventory-a")
    expect(page.locator("#decisions")).to_contain_text("pricing")


def test_placement_apply_and_reset_change_the_backend(page, api):
    open_tab(page, "control")
    page.fill("#placement-form input[name=retries]", "2")
    page.uncheck("#placement-form input[name=fallback]")
    with page.expect_request(lambda r: r.url.endswith("/api/placement") and r.method == "PUT") as req:
        page.click("#placement-apply")
    sent = body_of(req.value)
    assert sent["resilience"]["retries"] == 2 and sent["resilience"]["fallback"] is False
    expect(page.locator("#placement-msg")).to_contain_text("applied")
    snap = api.get("/api/placement").json()
    assert snap["source"] == "api" and snap["placement"]["resilience"]["retries"] == 2
    with page.expect_request(lambda r: r.url.endswith("/api/placement/reset") and r.method == "POST"):
        page.click("#placement-reset")
    expect(page.locator("#placement-msg")).to_contain_text("Reset to the files")
    assert api.get("/api/placement").json()["source"] == "file"


def test_links_apply_sends_whole_matrix(page, api):
    open_tab(page, "control")
    first = page.locator("#links-form input").first
    name = first.get_attribute("name")
    first.fill("33")
    with page.expect_request(lambda r: r.url.endswith("/api/links") and r.method == "PUT") as req:
        page.click("#links-apply")
    links = body_of(req.value)["link_rtt_ms"]
    assert links[name] == 33 and len(links) == 6
    expect(page.locator("#links-msg")).to_contain_text("Link delays applied")
    assert api.get("/api/placement").json()["link_rtt_ms"][name] == 33
    api.post("/api/placement/reset")


def test_fault_controls_inject_and_clear(page, api):
    open_tab(page, "control")
    expect(page.locator("#fault-target option")).to_have_count(3)
    page.select_option("#fault-target", "inventory-b")
    page.select_option("#fault-form select[name=mode]", "error")
    page.fill("#fault-form input[name=duration_s]", "20")
    with page.expect_request(lambda r: r.url.endswith("/api/faults") and r.method == "POST") as req:
        page.click("#fault-inject")
    assert body_of(req.value) == {"target": "inventory-b", "mode": "error", "duration_s": 20, "probability": 1}
    expect(page.locator("#faults")).to_contain_text("error · p=1")
    active = {f["target"]: f["active"] for f in api.get("/api/faults").json()["faults"]}
    assert active["inventory-b"]["mode"] == "error"
    with page.expect_request(lambda r: r.url.endswith("/api/faults") and r.method == "DELETE"):
        page.click("#fault-clear-all")
    expect(page.locator("#fault-msg")).to_contain_text("All faults cleared")
    assert all(f["active"] is None for f in api.get("/api/faults").json()["faults"])


# ------------------------------------------------------------------------------------------ errors
def test_rejected_input_shows_the_servers_reason(page, api):
    open_tab(page, "control")
    page.select_option("#fault-target", "inventory-a")
    # bypass the input's max attribute: the server must still refuse and the message must say why
    page.eval_on_selector("#fault-form input[name=duration_s]", "e => { e.removeAttribute('max'); e.value = '999'; }")
    page.click("#fault-inject")
    err = page.locator("#fault-msg .error")
    expect(err).to_contain_text("HTTP 422")
    expect(err).to_contain_text("duration_s")
    expect(err).to_contain_text("300")
    assert all(f["active"] is None for f in api.get("/api/faults").json()["faults"])


def test_server_error_and_network_failure_are_displayed(page):
    page.route("**/api/services", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body=json.dumps({"error": {"code": "internal_error", "message": "unexpected server error",
                                   "request_id": "rid-123"}})))
    page.goto(f"{BASE}/dashboard/#overview")
    expect(page.locator("#health .error")).to_contain_text("HTTP 500 internal_error: unexpected server error")
    expect(page.locator("#health .error")).to_contain_text("rid-123")
    expect(page.locator("#conn")).to_contain_text("controller error (HTTP 500)")
    page.unroute("**/api/services")
    page.route("**/api/gateway/orders", lambda route: route.abort())
    open_tab(page, "control")
    page.click("#order-send")
    expect(page.locator("#order-result .error")).to_contain_text("controller not reachable")


# ------------------------------------------------------------------------------------------ empty data
def test_empty_results_render_an_empty_state(page):
    page.route("**/api/experiments", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"experiments": []}'))
    open_tab(page, "experiments")
    expect(page.locator("#history")).to_contain_text("No experiments yet")
    expect(page.locator("#history")).to_contain_text("never shows example data")
    open_tab(page, "results")
    expect(page.locator("#result-select option").first).to_have_text("no finished experiments")
    expect(page.locator("#result-charts")).to_be_hidden()
    open_tab(page, "compare")
    page.click("#cmp-run")
    expect(page.locator("#cmp-warnings .error")).to_contain_text("Choose two finished experiments")


def test_experiment_without_completed_runs_has_no_chart(page):
    fake_id = "20260101T000000Z-smoke-000000"
    page.route("**/api/experiments", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"experiments": [
            {"experiment_id": fake_id, "scenario": "smoke", "status": "stopped",
             "started_at": "2026-01-01T00:00:00Z"}]})))
    page.route(f"**/api/experiments/{fake_id}/files/summary.json", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "experiment_id": fake_id, "status": "stopped", "runs_completed": 0, "runs_planned": 4,
            "validation": {"ok": True, "problems": []}, "per_run": [], "conditions": {}, "warnings": [], "notes": [],
            "comparisons": {"reference": "healthy", "method": "", "results": []}})))
    page.route(f"**/api/experiments/{fake_id}/files/manifest.json", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "experiment_id": fake_id, "scenario_id": "smoke", "environment_mode": "local", "git_commit": "x",
            "runs": [], "overrides": {}})))
    open_tab(page, "results")
    page.select_option("#result-select", fake_id)
    expect(page.locator("#result-head")).to_contain_text("no completed runs")
    expect(page.locator("#result-charts")).to_be_hidden()


# ------------------------------------------------------------------------------------------ one experiment
def wait_finished(api, exp_id: str, timeout: float = 120) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = api.get(f"/api/experiments/{exp_id}").json()
        if body["status"] in ("completed", "failed", "stopped"):
            return body
        time.sleep(1)
    raise AssertionError(f"experiment {exp_id} did not finish")


def test_reviewer_runs_one_experiment_from_the_dashboard(page, api, mode):
    scenario = "smoke" if mode == "local" else "smoke_simulated"
    open_tab(page, "experiments")
    page.select_option("#scenario-select", scenario)
    expect(page.locator("#scenario-detail")).to_contain_text("Condition")
    page.fill("#exp-form input[name=runs]", "1")
    page.fill("#exp-form input[name=label]", "browser test")
    expect(page.locator("#estimate")).to_contain_text("× 1 runs")
    with page.expect_request(lambda r: r.url.endswith("/api/experiments") and r.method == "POST") as req:
        page.click("#exp-start")
    sent = body_of(req.value)
    assert sent == {"scenario": scenario, "label": "browser test", "overrides": {"runs": 1}}
    expect(page.locator("#live-panel")).to_be_visible()
    expect(page.locator("#live-panel .badge.provisional")).to_be_visible()
    expect(page.locator("#live-counts svg")).to_be_visible(timeout=20000)
    exp_id = re.search(r"Started (\S+)", page.locator("#exp-msg").inner_text()).group(1)
    expect(page.locator("#exp-msg")).to_contain_text("completed", timeout=120000)
    expect(page.locator("#exp-msg")).to_contain_text("records validated")

    page.click("#exp-msg button:has-text('Open results')")
    expect(page.locator("#result-charts")).to_be_visible()
    expect(page.locator("#result-head")).to_contain_text("RECORDS VALIDATED")
    expect(page.locator("#result-head")).to_contain_text('"runs":1')
    expect(page.locator("#result-charts .badge.measured")).to_be_visible()
    summary = api.get(f"/api/experiments/{exp_id}/files/summary.json").json()
    conditions = list(summary["conditions"])
    expect(page.locator("#metric-chart svg circle.dot")).to_have_count(len(conditions))  # one dot per run
    for c in conditions:
        expect(page.locator("#result-table")).to_contain_text(c)
    if mode == "simulated":
        expect(page.locator("#result-table")).to_contain_text("100 ms (configured)")
    # timeline of the run with a fault (local smoke) or the delayed run (simulated smoke)
    run_id = "error-fault-r0" if mode == "local" else "d100-r0"
    page.select_option("#run-select", run_id)
    page.click("#timeline-load")
    expect(page.locator("#timeline-counts svg rect.bar").first).to_be_visible()
    if mode == "local":
        expect(page.locator("#timeline-counts")).to_contain_text("configured window, actual start")
        expect(page.locator("#timeline-counts svg rect.st-bad-fill").first).to_be_visible()
    # every export link downloads the real file
    hrefs = page.locator("#export-links a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert len(hrefs) == 5
    for href in hrefs:
        r = api.get(href)
        assert r.status_code == 200 and len(r.content) > 0, href
    csv_rows = api.get(f"/api/experiments/{exp_id}/files/observations.csv").text.count("\n") - 1
    assert csv_rows == summary["validation"]["rows_csv"]
    assert page.console_errors == []


def test_compare_two_experiments(page, api, mode):
    scenario = "smoke" if mode == "local" else "smoke_simulated"
    ids = []
    for runs in (1, 2):
        r = api.post("/api/experiments", json={"scenario": scenario, "label": f"compare {runs}",
                                               "overrides": {"runs": runs}})
        assert r.status_code == 202, r.text
        ids.append(r.json()["experiment_id"])
        assert wait_finished(api, ids[-1])["status"] == "completed"
    open_tab(page, "compare")
    page.select_option("#cmp-a", ids[0])
    page.select_option("#cmp-b", ids[1])
    page.select_option("#cmp-metric", "success_rate")
    page.click("#cmp-run")
    expect(page.locator("#cmp-warnings")).to_contain_text("different overrides")
    expect(page.locator("#cmp-chart .legend")).to_contain_text(f"A: {ids[0]}")
    expect(page.locator("#cmp-chart .legend")).to_contain_text(f"B: {ids[1]}")
    expect(page.locator("#cmp-chart svg circle.dot")).to_have_count(2 * (1 + 2))  # 2 conditions x (1 + 2) runs
    expect(page.locator("#cmp-table tbody tr")).to_have_count(2)
    expect(page.locator("#cmp-table")).to_contain_text("No significance test")
