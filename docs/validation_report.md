# Validation Report (Phase 7)

**Scope.** This report covers what has been executed and verified for SETU-X up to the end of Phase 7: the application, the experiment harness and the dashboard. It does **not** report experimental findings; those come from the Phase 8 runs. Every "PASS" below names the log that shows it. A check that was not executed is listed as not executed, never as passing.

**Versions.**
- Code: SETU-X 0.6.0, verified on commit `eb4d2f0` (Linux).
- Workspace toolchain: Docker Engine 29.x with Compose 5.x, PowerShell 7.6.8.
- Parv's Windows machine: Docker Desktop 29.4.2 with Compose 5.1.3, PowerShell 7.6.8.

## 1. One-command verification from a clean checkout

`pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1` does the following:
- clones the committed `HEAD` into a temporary folder;
- runs it as a separate Compose project (`setu-x-verify`, ports 18080/18090, its own results volume);
- runs the 18 checks below;
- removes everything it created.

Uncommitted files are therefore never part of a verification.

| Check | What it proves | Linux (`eb4d2f0` + mapping fix) | Windows |
|---|---|---|---|
| V1 | Clean clone of `HEAD`; nothing uncommitted is used | PASS | pending |
| V2 | No secret patterns (private keys, AWS/GitHub/Slack/Google/OpenAI-style tokens, hard-coded passwords), no `.env`/key files tracked; any committed `results/` directory is complete and validated | PASS (173 tracked text files; patterns tested against a planted key file) | pending |
| V3 | `docker compose config` | PASS | pending |
| V4 | Images build from the clean checkout | PASS | pending |
| V5 | Config validator accepts `configs/` and rejects `tests/fixtures/bad_config` | PASS (exit 0 / exit 1) | pending |
| V6 | Unit + contract + integration tests | PASS (336 passed) | pending |
| V7 | Lint (`ruff`) | PASS | pending |
| V8 | Clean start; README quick start: `ps`, the README `curl` order (golden values 8 in stock, 11000 cents), `logs gateway` contains a JSON line with that request ID | PASS | pending |
| V9 | End-to-end tests against the running stack | PASS (10) | pending |
| V10 | Dashboard in Chromium (Playwright) | PASS (13) | pending |
| V11 | Baseline experiment (A_baseline, 2 runs × 20 s, overrides recorded): records validated; manifest's scenario SHA-256 and git commit match the checkout; p50/p95 recomputed from the CSV match `summary.json`; p99 withheld (n < 1,000) | PASS | pending |
| V12 | Failure experiment (C_service_failure, error and hang, 1 run × 25 s): failures only inside the actual fault window, none before it; hang failures cut at the 800 ms timeout (slowest 877 ms); recovery measured; records validated | PASS | pending |
| V13 | Traceability: a measured request ID appears in the gateway and inventory logs; fault injection/clearing events are recorded per run with the runner's schedule | PASS | pending |
| V14 | Runtime configuration: an invalid placement is refused with no effect; a valid one reaches the gateway and changes behaviour (sequential calls, 1 retry); reset restores the files | PASS | pending |
| V15 | Restart: a controller restart reverts runtime placement to the files (documented limitation) while orders keep working; full stop/start recovers; results survive restarts | PASS | pending |
| V16 | Container-level failure (`fault_container.ps1`): stopped primary → served by inventory-b; primary serves again after restart | PASS | pending |
| V17 | Every README command (20) is executed in this run or backed by a PASS acceptance log | PASS | pending |
| V18 | Hardening intact; all 6 containers exit 0 on stop; containers, network and verification volume removed | PASS | pending |

Log: `docs/evidence/verify_linux.log`. Windows: please run `scripts/verify.ps1` and send `docs/evidence/verify_windows.log`. Until then, the Windows column is **pending**.

In the build workspace, `docker compose build` cannot run (it needs host networking for the proxy). There the images were built with the same Dockerfile by a workspace script, and V8 started the stack with `docker compose up -d` instead of `up --build -d`. The log states this. On Windows the README command runs unchanged.

## 2. Acceptance history per phase (all executed)

| Phase | Linux | Windows (Parv's PC) |
|---|---|---|
| 2 Services and containers | PASS (`phase2_acceptance_linux.log`) | PASS 14/14 (`phase2_acceptance_windows.log`) |
| 3 Placement, routing, adapters | PASS 15/15 | PASS 15/15 |
| 4 Injection, timeouts, retries, breaker, fallback | PASS 15/15 | Run 1: 14/15 (check bug, below); re-run PASS 15/15 |
| 5 Experiment harness | PASS 15/15 | Run 1: 14/15 (check bug, below); re-run PASS 15/15. Injected 100 ms measured as +106.8 ms |
| 6 Dashboard | Run 1: 14/15 (test not updated, below); re-run PASS 15/15 | PASS 15/15, including 24 browser-test executions in local and simulated mode |
| 7 Clean-checkout verification | PASS 18/18 | pending |

Regression runs: Phase 2 on Phase 3, Phase 3 on Phase 4, Phase 4 on Phase 5, Phase 5 on Phase 6, all PASS (`docs/evidence/*_regression_*.log`).

## 3. Test inventory

| Suite | Count | Where it runs |
|---|---|---|
| Unit | 195 | `tests` container (no stack needed) |
| Contract (provider adapters) | 54 | same |
| Integration (in-process services, runner, dashboard backend) | 87 | same |
| End-to-end (running stack) | 10 | `tests` container on the Compose network |
| Browser (dashboard in Chromium) | 13 | `browser-tests` container (profile `browser`) |
| JavaScript helpers | 7 | `node --test` where Node is installed; the same helpers are cross-checked against the Python statistics in the browser suite |

**Mutation checks** (a deliberate bug is introduced; the suite must fail):
- Phase 4: 9 of 9 caught.
- Phase 5: 15 of 15 caught (bootstrap without resampling needed a new test).
- Phase 6: 6 of 6 caught on the dashboard.
- Phase 7: the secret scanner detected all three planted patterns (key, private-key header, password) in a test file.

## 4. Phase 7 acceptance criteria

| Criterion (project brief) | Status | Evidence |
|---|---|---|
| All required automated tests pass, or failures are documented | Met | V6, V9, V10; no known failing test |
| No critical request path knowingly broken | Met | V8 (order path), V12/V16 (failure paths), V14 (config path) |
| Clean startup, experiment, export and shutdown cycle succeeds | Met | V8 → V11/V12 (records downloaded and reconciled) → V18 |
| Results traceable to their experiment configurations | Met | V11: manifest holds scenario text + SHA-256 (matches the file), overrides, git commit, seeds, placement SHA-256 per run; V13: request IDs → service logs |
| All commands in the README tested | Met | V17: 20 commands. Most were executed in the run; the per-phase acceptance commands are backed by PASS logs. Where the workspace could not run a README command as written (see §1), the log says so |
| No credentials, private keys or fabricated experimental results | Met | V2; `results/` holds no committed results yet, and none may be committed without validation |

## 5. Deliverable checklist (project brief §15): status at the end of Phase 7

| Item | Status |
|---|---|
| Runnable microservices; Docker Compose setup | Done (Phases 2–6) |
| Provider-neutral interface | Done: adapter contract with local, simulated and config-only AWS/Azure/GCP adapters; real-cloud deployment **not implemented** (`docs/cloud_setup.md`) |
| Clearly labelled simulated environments | Done (validator enforces `-simulated` names; notices in API, dashboard, docs) |
| Routing and placement logic; fault and latency injection; timeouts and resilience policies | Done (Phases 3–4) |
| Workload generator; metrics and data export | Done (Phase 5) |
| Interactive dashboard | Done (Phase 6) |
| Verified bibliography; literature review; research-gap matrix; explicit hypotheses; methodology | Done (Phase 1, methodology Phase 5; 30/30 DOIs Crossref-verified) |
| Actual experiment artifacts; evidence-based analysis | **Phase 8** |
| Limitations and threats to validity | Partly: §6 below and `docs/methodology.md` §10; full write-up with results in Phase 8 |
| Unit, integration, end-to-end tests; clean startup and shutdown; validated exports; validation report | Done (this report) |
| Security and secret checks | Done (V2 in `verify.ps1`); repeated in CI in Phase 9 |
| GitHub Actions workflow | **Phase 9** |
| Editable `.pptx`, citations, results, under 30 minutes, render check | **Phase 10** (a 6-slide progress deck exists; it is not the final 5-slide deck) |
| `voiceover.md` | **Phase 11** (written only after the deck is final) |

## 6. Defects found during development (all fixed, with evidence kept)

| Phase | Defect | Kind | How found | Fix |
|---|---|---|---|---|
| 2 | Fault handling placed before request-ID middleware would have produced responses without IDs | code | review | moved to a route dependency |
| 3–4 | Sequential call mode logged a pricing routing decision even when pricing was never called | code | test | route pricing only when called |
| 4 | Circuit-breaker probe ownership: a request could release another request's half-open probe | code | review | explicit `acquire()` / `release_probe()` |
| 4 | Acceptance C10 compared a host stopwatch with a container timer (different clock domains on Docker Desktop) | check | Windows run 1 | gateway-measured lower bound; led to the runner container design |
| 5 | Recovery metric missed faults shorter than the window when they started between grid points | code | integration test | windows overlapping the fault |
| 5 | Runner reported `completed` before `summary.json` existed (≈1 in 9 runs) | code | repeated test runs | `analysing` status until written |
| 5 | After an experiment the gateway kept the last run's placement until its next poll (≤ 2 s) | code | Phase 4 regression C7 | runner syncs the gateway on restore |
| 5 | Acceptance D3 parsed `docker compose port` output, which differs on Windows Compose 5.1.3 | check | Windows run 1 | decide from `docker inspect` |
| 6 | Dashboard success messages were wiped by the refresh that followed them; optional parts rendered as "null" | code | browser tests / screenshots | `put()` helper; refresh without clearing |
| 6 | e2e test still expected 4 services after the runner joined `/api/services` | test | acceptance E5 | test updated |

## 7. Known limitations (none blocks Phase 8)

| Limitation | Severity | Effect | Mitigation |
|---|---|---|---|
| All services, the load generator and every "environment" share one host | High for external validity | Results describe this machine; no claim about AWS/Azure/GCP | Labels and notices everywhere; real-cloud path documented, not implemented |
| Simulated link delay is constant (no jitter, loss or bandwidth) | Medium | Delay effects are idealised | Injected delay exported separately; measured vs configured shown |
| Faults are application-level (health stays up) | Medium | Health checks do not reflect injected faults | Container stop covered by `fault_container.ps1` (V16) |
| Runtime placement lives in controller memory | Low | A controller restart reverts to the files (V15) | Documented; experiments reset placement per run anyway |
| No authentication on the control surface | Low (local only) | Anyone with access to 127.0.0.1 can change settings | Published on 127.0.0.1 only; Host and Origin checks; bounded faults and scenarios |
| Recovery time includes up to about 1 s of window lag | Low | Instant recovery reads as ≈ 0.9–1.1 s | Parameters reported with every value |
| Failure-detection times differ between Docker platforms | Medium | Connect errors vs connect timeouts | Platform recorded in the manifest; Phase 8 runs on one machine |
| Few runs per condition (3–5) | Medium | Coarse bootstrap intervals | Per-run values always shown; `runs` can be raised |
| Experiments started from the dashboard do not record the git commit | Low | Weaker traceability for those runs | Phase 8 uses `scripts/run_experiment.ps1`, which records it |
| Browser-test image is large (~3.5 GB) | Low | First run is slow to download | `-SkipBrowser` exists, but the verdict is then INCOMPLETE |

## 8. How to reproduce

```powershell
pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1          # about 15 minutes; docs/evidence/verify_<platform>.log
pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1 -SkipBrowser   # no 3.5 GB image; verdict INCOMPLETE
```
