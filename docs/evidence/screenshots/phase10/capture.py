"""Phase 10: genuine dashboard captures (simulated mode) showing the Phase 8 Windows campaign records."""
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8090"; OUT = "docs/evidence/screenshots/phase10"
G = "20261026T091145Z-G_hybrid_fallback-08be77"; D = "20261026T082525Z-D_policies_hang-929ca0"
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=2)
    pg.goto(f"{B}/dashboard/#overview"); pg.wait_for_selector("#health tbody tr >> nth=4"); pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{OUT}/01_overview_simulated_mode.png", full_page=True)
    for eid, metric, run, name in [(G, "success_rate", "fallback-breaker-r0", "02_results_G_hybrid_fallback.png"),
                                    (D, "success_rate", "timeout-retry-breaker-r0", "03_results_D_policies_hang.png")]:
        pg.goto(f"{B}/dashboard/#results"); pg.wait_for_selector(f"#result-select option[value='{eid}']", state="attached")
        pg.select_option("#result-select", eid); pg.wait_for_selector("#metric-chart svg"); pg.wait_for_timeout(500)
        pg.select_option("#metric-select", metric); pg.wait_for_timeout(500)
        pg.select_option("#run-select", run); pg.click("#timeline-load"); pg.wait_for_selector("#timeline-latency svg"); pg.wait_for_timeout(1000)
        pg.screenshot(path=f"{OUT}/{name}", full_page=True)
    b.close()
