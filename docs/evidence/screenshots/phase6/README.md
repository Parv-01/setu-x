# Phase 6 dashboard screenshots (genuine captures)

- **How they were made:** captured from the real running stack by headless Chromium (Playwright 1.63, 1280×860 viewport, scale 1.5, full page). The capture script drives the dashboard exactly as a person would: clicks, form inputs, and starting an experiment.
- **Where they were made:** in the Linux build workspace, local mode, on commit `4f2d752` (`setu-x:0.6.0` images), 2026-10-25.
- **What the data is:** two `smoke` scenario runs (`20261025T131106Z-smoke-f0a950`, 2 runs per condition; `20261025T131147Z-smoke-855b83`, 1 run). These are harness checks, not experiment results: they are too short for p95/p99 and are not used in any finding.
- **What they contain:** no credentials, tokens or personal data. The footer shows the author name as intended.

| File | Shows |
|---|---|
| `01_overview_health_graph.png` | All 6 containers ready; dependency graph with simulation labels; adapters "not_configured" |
| `02_control_fault_fallback.png` | An `error` fault on inventory-a; the next test order served by inventory-b via fallback (resilience event shown) |
| `03_experiment_running_live.png` | Experiment started from the dashboard, captured during the `error-fault` run: provisional per-second outcomes with the configured fault window |
| `04_results_run_level_and_timeline.png` | Finished experiment: validated records, run-level success rates, table, comparisons, per-second timeline of `error-fault-r0` |
| `05_compare_two_experiments.png` | Two experiments compared, with the "different overrides" warning |

Screenshots taken on Parv's Windows machine are preferred for the final presentation (Phase 10).
