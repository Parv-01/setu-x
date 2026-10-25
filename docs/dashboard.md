# Dashboard

The dashboard is a static HTML/JavaScript page served by the controller. Open **http://127.0.0.1:8090/** after `docker compose up -d`.

- It has no build step and no external scripts, styles or fonts, so it works offline.
- It talks only to the controller (same origin). The controller passes read-only gateway views and experiment calls through to the gateway and the runner, which are not published to the host.
- No result is hard-coded. With no finished experiments it shows an empty state.

Code: `dashboard/index.html`, `dashboard/css/app.css`, `dashboard/js/{app,api,charts,stats}.js`.

## How values are labelled

| Badge | Meaning |
|---|---|
| LIVE | Read from the running services just now (health every 5 s, routing decisions, events). |
| PROVISIONAL | Per-second counts from an experiment that is still running. Not validated and never used as a result. |
| HISTORICAL | Read from a finished experiment's files (`summary.json`, `observations.csv`), with its ID and finish time. |
| MEASURED | Latencies and counts measured by the load generator inside the Compose network. |
| CONFIGURED | A setting, not a measurement: link delays, timeouts, fault windows. Drawn as dashed reference lines or shaded bands with a "(configured)" label. |
| SIMULATED LABEL | An environment label on this Docker host. No cloud resources are involved. |

The page header always shows the stack's mode (LOCAL or SIMULATED). In local mode, link delays are stored but not injected, and the page says so wherever they appear.

## Tabs

- **Overview.** Readiness of all six containers. The dependency graph shows the gateway, inventory primary and secondary, and pricing, with their environment labels and configured link delays. Below it are the current resilience settings and the status of the real-cloud adapters (configuration only).
- **Control.**
  - Edit the placement: call mode, routing policy, each instance's environment label, role, weight and enabled flag, and the resilience settings. Then **Apply** it, or **Reset to files**.
  - Edit link delays.
  - Inject and clear bounded faults.
  - Send one test order and see which instance served it, the gateway-measured time and the configured injected delay.
  - Inspect routing decisions, circuit breakers and resilience events.
- **Experiments.**
  - Choose a scenario file. Its comment, conditions and scheduled faults are shown.
  - Optionally override rate, warm-up, measurement time, runs per condition and (simulated scenarios) default link delays. All overrides are bounded by the same rules as the files.
  - Start. While it runs, a provisional live view shows per-second outcomes and median latency for the current run, plus a summary of each finished run. **Stop** keeps completed runs.
  - The history lists every experiment in the results volume.
- **Results.**
  - For a finished experiment: status, validation, warnings, overrides and measurement notes.
  - One run-level chart per metric: a dot for each run, the median of the runs, and a bootstrap 95% interval over runs.
  - A table, the comparisons computed by the runner, and a per-second timeline of any run, read from `observations.csv`, with the fault window at its actual injection time.
  - Download buttons for all five files.
- **Compare.** Two finished experiments side by side for any metric. Warnings appear when scenario, mode, code version, overrides or completion differ. Intervals are within each experiment; no cross-experiment significance test is made.
- **Method & limits.** The labels above, the method in brief and the limitations.

## Run one experiment from the dashboard (reviewer walkthrough)

1. `docker compose up -d`, then open http://127.0.0.1:8090/. Overview should show six containers, all **ready**.
2. Go to **Experiments**. Choose `smoke — Harness smoke test (local mode)`. Optionally set *Runs per condition* to 1 and a label.
3. Press **Start experiment**. The live panel appears. The `error-fault` condition shows a red block of failures where its fault is scheduled.
4. When the message says *completed … records validated*, press **Open results**.
5. In **Results**, change the metric, then choose run `error-fault-r0` and press **Load timeline**: the failures sit inside the shaded fault window.
6. Use the download buttons to export `observations.csv` and `summary.json`.

For simulated-mode scenarios (B, F, G, `smoke_simulated`), restart the stack with `SETU_MODE=simulated` first. The dashboard refuses to start a scenario whose mode does not match the stack and says why.

## Safety

The controller has no authentication. It is a **local development control surface**, published on 127.0.0.1 only. Because a browser page now uses it, the controller also:

- **Host check:** refuses requests whose `Host` header is not `127.0.0.1`, `localhost`, `::1` or `controller`. This is a defence against DNS rebinding.
- **Origin check:** refuses state-changing requests (POST/PUT/DELETE) whose `Origin` is another website. A page on some other site open in the same browser therefore cannot inject faults or start experiments.
- **Content-Security-Policy:** serves the dashboard with a strict policy: same-origin scripts, styles and connections only, and no framing.

Fault injection stays bounded and allow-listed as before (`docs/architecture.md` §5). Experiments can only run scenario files from `configs/scenarios/`, within the scenario bounds.

## Tests

- **Backend:** `tests/integration/test_dashboard_backend.py` covers static serving and CSP headers, origin and host checks, gateway views, bounded overrides and the live endpoint.
- **Pure JavaScript helpers:** `node --test dashboard/tests/*.test.mjs`, run where Node 18+ is available. Covers percentiles identical to the Python implementation, CSV parsing, per-second buckets and comparison warnings.
- **Real browser:** `tests/browser/test_dashboard.py` drives Chromium through Playwright against the running stack, with 12 tests:
  - every control sends the expected request and the backend state changes;
  - server errors, network failures and rejected input are displayed with their reason;
  - empty data gives an empty state;
  - one experiment runs end to end from the dashboard;
  - two experiments are compared.

  Run it with the stack up: `docker compose --profile browser run --rm browser-tests`. The image is about 3.5 GB and is downloaded once.
