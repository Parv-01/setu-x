# Phase 10 dashboard screenshots (genuine captures)

- **How they were made:** `capture.py` in this folder drives the real dashboard with headless Chromium (Playwright 1.63, 1280×860 viewport, scale 2, full page).
- **Where:** the Linux build workspace, stack in **simulated** mode (`SETU_MODE=simulated docker compose up -d`), images `setu-x:0.6.0` built from commit `0bea98b`, 2026-10-26.
- **What the data is:** the eight Phase 8 experiment folders from Parv's Windows PC (`results/2026102*`, campaign `20261026T081408Z`), copied unchanged into the runner's results volume. The dashboard re-reads and displays those records; nothing was re-run or edited. The service-health rows and probe times on the overview are live values from the workspace stack.
- **What they contain:** no credentials, tokens or personal data. The footer shows the author name as intended.

| File | Shows | Used in |
|---|---|---|
| `01_overview_simulated_mode.png` | Six containers ready, environment labels, dependency graph with configured link delays, real-cloud adapters `not_configured` | Slide 2 (cropped to the graph: `presentation/assets/shot_dependency_graph.png`) |
| `02_results_G_hybrid_fallback.png` | Experiment G results: run-level success rates, table, comparisons, per-second timeline of `fallback-breaker-r0` | Slide 3 (cropped to the timeline: `presentation/assets/shot_G_timeline.png`) |
| `03_results_D_policies_hang.png` | Experiment D1 results with the `timeout-retry-breaker-r0` timeline | not used in the deck |

To capture on Windows instead: start the stack, open `http://127.0.0.1:8090/dashboard/`, choose the experiment on the Results tab and use the browser's full-page screenshot (DevTools → "Capture full size screenshot").
