# SETU-X Handover (final)

**Last updated:** 2026-10-26 · **Status:** all phases complete (0–10). At Parv's request the voiceover and final handover, planned as Phase 11, were done inside Phase 10.
**Repository:** https://github.com/Parv-01/setu-x (pushed 2026-10-26, branch `main`) · **License:** Apache-2.0 for code; CC BY 4.0 for docs, figures, presentation and results.

## Final deliverables

| Deliverable | Where | Checked by |
|---|---|---|
| Runnable platform (6 containers, dashboard) | `compose.yaml`, `src/`, `dashboard/` | 337 unit/contract/integration, 10 e2e, 13 browser, 7 JS tests; `scripts/verify.ps1` 18/18 on Linux and Windows |
| Research design and literature | `docs/research_questions.md`, `docs/literature_review.md`, `docs/references.bib` | 30/30 DOIs Crossref-verified (`scripts/verify_references.py`) |
| Method | `docs/methodology.md` | Phase 5 mutation checks (15/15 caught) |
| Experiment results (raw records + analysis) | `results/2026102*/`, `results/analysis/`, `results/campaign_20261026T081408Z.json` | re-validated by `scripts/analyze_results.py`; `scripts/repo_check.py` |
| Findings, limitations, threats to validity | `docs/findings.md` (covers the plan's `experimental_results.md`, `limitations.md` and `threats_to_validity.md` in one file) | every number checked against `results/analysis/*.json` |
| Validation report | `docs/validation_report.md` | — |
| CI, security, citation, release | `.github/workflows/ci.yml`, `SECURITY.md`, `CITATION.cff`, `docs/release_checklist.md` | CI green on GitHub for commit `0bea98b` |
| Final presentation (5 slides) | `presentation/setu_x.pptx` (+ `setu_x.pdf` render), generator `presentation/build_deck.js` | slide limit asserted in code; OOXML validation; LibreOffice render inspected |
| Voiceover | `presentation/voiceover.md` (also in the speaker notes) | `presentation/check_voiceover.py`: 831 words ≈ 5.5–5.9 min, titles match |
| Genuine screenshots | `docs/evidence/screenshots/phase6/`, `docs/evidence/screenshots/phase10/` (with capture scripts and READMEs) | no credentials or personal data |

## How to pick this up

```powershell
git clone https://github.com/Parv-01/setu-x; cd setu-x
docker compose up --build -d --wait          # local mode; dashboard at http://127.0.0.1:8090/
pwsh -File scripts/verify.ps1                # full clean-clone verification (18 checks)
pwsh -File scripts/run_all_experiments.ps1   # new campaign (~65 min), then:
python scripts/analyze_results.py results/<new ids>/ --out results/analysis
cd presentation; npm ci; node build_deck.js  # rebuild the deck from results/
python presentation/check_voiceover.py       # needs python-pptx
```

## Completed

- **Phase 0 (PASS):** environment inventory and plan; Windows Docker confirmed (29.4.2, Compose 5.1.3).
- **Phase 1 (PASS):** RQ, H1–H5, literature review and matrix, verified bibliography, architecture.
- **Phase 2 (PASS, Linux + Windows):** gateway, inventory, pricing; hardened containers. Windows log `docs/evidence/phase2_acceptance_windows.log` (14/14).
- **Phase 3 (PASS, Linux + Windows):** placement and environment config with validation; adapter contract (local + AWS/Azure/GCP config-only skeletons); routing policies; controller. Windows log `docs/evidence/phase3_acceptance_windows.log` (15/15, commit c5b9121).
- **Phase 4 (PASS, Linux + Windows):** Windows log `docs/evidence/phase4_acceptance_windows.log` (15/15 on 9766ac5 after the C10 check fix).
  - `setu_x/resilience.py`: full-jitter backoff, circuit breaker (explicit probe ownership, release of unused probes), breaker registry, bounded event log.
  - `setu_x/faults.py`: bounded, seeded, self-expiring `error`/`slow`/`hang` faults as a route dependency; `/admin/fault` on inventory and pricing.
  - Gateway:
    - `gateway/resilient.py`: retries, breaker, fallback over routing candidates, deadline-aware backoff.
    - Simulated link delay in `client.py`.
    - Responses now include `resilience`, `tried`, `served_instance` and `injected_delay_ms`.
    - New endpoints `/api/resilience/events` and `/api/resilience/breakers`.
    - Breakers reset when the placement or link matrix changes.
  - Controller: `PUT /api/links` (runtime link matrix; reset restores the file), allow-listed fault proxy (`/api/faults`), event log (`/api/events`).
  - `scripts/fault_container.ps1`: real container stop and start, bounded and allow-listed.
  - `scripts/phase4_acceptance.ps1`: 15 checks. PowerShell scripts now keep JSON dates as strings (`-DateKind String`); the Windows Phase 3 log showed PowerShell converting UTC timestamps to +05:30.
  - Version 0.4.0.

- **Phase 5 (PASS, Linux build workspace):**
  - `setu_x/experiments/`: `scenario.py` (bounded scenario schema; `defaults` merged into conditions; every condition validated against the config validators), `schedule.py` (constant/Poisson open-loop schedules, randomized/interleaved/blocked run order, per-run seeds), `loadgen.py` (open-loop generator, in-flight cap with recorded drops, latency from the scheduled time), `observation.py` (row schema, outcome classes), `records.py` (writer, re-read validation), `metrics.py` (type-7 percentiles with the n ≥ 10/(1−q) rule, rates, recovery time), `statistics.py` (run-level medians, seeded bootstrap over runs, comparisons; percentiles never averaged), `runner.py` (per-run isolation, scheduled faults, events, summary), `cli.py`.
  - New `runner` container (`setu_x.services.experiments`): internal only, 1.0 CPU, named volume `setu-results`. The controller proxies `/api/scenarios` and `/api/experiments*`.
  - Gateway: `POST /api/resilience/reset {seed}` (sync placement now, reset breakers and routing state, reseed jitter); event endpoints accept `?since=` and report `last_seq`/`oldest_seq`; event log 20,000 entries.
  - Scenario files A–G plus two smoke scenarios in `configs/scenarios/`. `scripts/run_experiment.ps1` (start, progress, download, optional host-side `docker stats`). `docs/methodology.md`. References added and Crossref-verified: hyndman1996sample, efron1979bootstrap (30/30 DOIs verified).
  - Architecture change: the runner moved out of the controller into its own container because of the clock-domain finding (Phase 4 finding 6). `docs/architecture.md` and the figure are updated.
  - `scripts/phase5_acceptance.ps1`: 15 checks, including independent recomputation of counts, success rates and medians from the CSV.
  - Version 0.5.0.

- **Phase 6 (PASS, Linux build workspace):**
  - `dashboard/`: static HTML/CSS/JS served by the controller at `/dashboard/`. No CDN, strict CSP, SVG charts with units.
    - Tabs: overview (health, dependency graph, adapters), control (placement, links, bounded faults, test order, routing decisions, breakers, events), experiments (scenario detail, bounded overrides, provisional live view, history), results (run-level dot plots with bootstrap CIs, table, comparisons, per-second timeline from the CSV, export), compare, method and limits.
    - Every value is labelled LIVE / PROVISIONAL / HISTORICAL / MEASURED / CONFIGURED / SIMULATED LABEL. Empty and error states are handled; no results are hard-coded.
  - Controller:
    - Serves the dashboard, plus read-only gateway views and a test-order proxy, and the live-experiment proxy.
    - **Browser protections:** a Host allow-list (against DNS rebinding) and an Origin check on state-changing requests (another website cannot inject faults); the runner appears in service health.
  - Runner: bounded overrides (rate, warm-up, duration, runs, default links), validated by the scenario schema and recorded in the manifest; provisional live view.
  - Tests:
    - Backend integration tests.
    - JS unit tests (`node --test dashboard/tests/*.test.mjs`, 7).
    - 12 Playwright browser tests in a pinned image, compose profile `browser`, run in local and simulated mode.
  - Acceptance helpers gained an explicit SKIP status. A skipped check is never PASS; the verdict becomes INCOMPLETE.
  - `scripts/phase6_acceptance.ps1` (15 checks). `docs/dashboard.md`. Genuine screenshots in `docs/evidence/screenshots/phase6/`. Version 0.6.0.

- **Phase 7 (PASS, Linux + Windows):** `scripts/verify.ps1` (V1–V18, clean clone, own Compose project and ports) 18/18 on both; `docs/validation_report.md`; README commands covered (V17).

- **Phase 8 (complete, Parv's Windows PC):**
  - Campaign `results/campaign_20261026T081408Z.json`: commit `bfe122c`, 64.8 min, i5-13500, Docker Desktop 29.4.2 (20 CPUs, 7.59 GiB), power plan Balanced. 8/8 experiments completed and validated on the device and again here.
  - `scripts/analyze_results.py` (matplotlib, `requirements/analysis.lock`) re-validates every record and writes `results/analysis/<scenario>.json` and figures.
    - Fix during analysis: inventory calls are derived as total attempts − 1, valid only in parallel mode with healthy pricing; the script refuses other configurations.
    - E reads `docker_stats.csv` for per-container CPU.
  - `docs/findings.md`: H1, H3, H4 and H5 supported; H2 supported with a recovery cost (breaker); E shows congestion collapse at 80 req/s (gateway at its 0.5 CPU limit). Threats to validity listed. Every number was checked against `results/analysis/*.json`.
  - Pilot results (Linux, 1 run per condition) were not committed.

- **Phase 9 (complete):** CI workflow (hygiene scan, ruff, config checks, tests, JS tests, docs; Compose stack + loopback-port check + e2e), `scripts/repo_check.py`, `SECURITY.md`, `CITATION.cff`, Dependabot, release checklist. License Apache-2.0 + CC BY 4.0 (SPDX texts). Pushed to https://github.com/Parv-01/setu-x on Parv's authorisation; first CI run passed both jobs. From 2026-10-26, commits are authored as parvagarwal9759@gmail.com (earlier history kept, by Parv's decision).
- **Phase 10 (complete):**
  - `presentation/build_deck.js` (pptxgenjs 4.0.1, pinned in `presentation/package-lock.json`) builds `setu_x.pptx`; it throws if the deck exceeds five slides. All numbers on the slides are read from `results/analysis/*.json` and the campaign file at build time.
  - Slides: (1) problem, motivation with citations, research question, H1–H5; (2) research gap G1–G4, system, modes, safety and cost, genuine dependency-graph capture; (3) method, campaign facts, testing gates, genuine capture of a Windows-campaign run; (4) verdict table H1–H5 + E with native charts for H1 and H2; (5) takeaways, limits, next steps, 13 references.
  - New screenshots `docs/evidence/screenshots/phase10/` (dashboard displaying the committed Windows records; workspace stack in simulated mode).
  - `presentation/voiceover.md` written after the deck was final; also placed in the speaker notes.
  - QA: OOXML validation PASS; rendered via LibreOffice and inspected (fixed a title overlap, citation line breaks, chart axes, a lost bullet); rebuilt from a clean `npm ci` with identical slide text.

## Test status (executed in the build workspace)

| Check | Result |
|---|---|
| Unit + contract + integration (Python 3.11.17) | 321 passed (repeated runs; one race found and fixed, see below) |
| e2e against the running stack | 10 passed (local mode) |
| Mutation checks, Phase 5 (15): nearest-rank percentile, low p99 threshold, dropped excluded from denominators, warm-up counted, closed-loop generator, latency from actual send, in-flight cap ignored, duplicate IDs unchecked, no reset before a run, gateway reset not reseeding, percentiles averaged across runs, conditions not validated, bootstrap without resampling, mode mismatch allowed, fault outside window allowed | all 15 caught. The bootstrap one initially survived, so a test was added |
| `ruff check src tests` | clean |
| `phase5_acceptance.ps1` (pwsh 7.6.8, Linux) on 14df432 | 15/15 PASS (`docs/evidence/phase5_acceptance_linux.log`). Smoke checks: median(d100) − median(d0) = 101.7 ms for an injected 100 ms; fault injected < 1 ms late; failures only inside the fault window |
| Phase 4 regression on 14df432 | 15/15 PASS (`docs/evidence/phase4_regression_on_phase5_linux.log`); an earlier run on 1d64f6e failed C7, see below |
| Phase 6: unit + contract + integration | 336 passed, 1 skipped (browser tests are skipped in the test image; they run in their own image) |
| Phase 6: JS unit tests (Node 22) | 7 passed |
| Phase 6: browser tests (Chromium, Playwright 1.63) | 12 passed in local mode and 12 in simulated mode (inside the Compose network and from the host) |
| Phase 6: mutation checks on the dashboard (6): fallback checkbox ignored, quantity sent as a string, request ID dropped from errors, fault posted to the wrong endpoint, overrides not sent, empty history blank | all 6 caught |
| `phase6_acceptance.ps1` (pwsh 7.6.8, Linux) on 4f2d752 | 15/15 PASS, 0 skipped (`docs/evidence/phase6_acceptance_linux.log`). Run 1 on 53c0782 failed E5, see below |
| Phase 5 regression on 0afc449 (includes the D3 fix) | 15/15 PASS (`docs/evidence/phase5_regression_on_phase6_linux.log`) |
| Windows (Parv's PC), run 1 on 3050b87 | 14/15: D3 failed because of the check, not the stack. `docker compose port` prints `invalid IP:0` for an unpublished port on Compose 5.1.3/Windows (`:0` on Linux); the container inspection in the same check showed no host port. D3 now decides from `docker inspect` (Ports + PortBindings) only; verified that it fails when the runner is published. Evidence: `docs/evidence/phase5_acceptance_windows_run1_d3_check_bug.log`. Re-run pending |

Phase 6, first acceptance run (53c0782): 14/15. E5 failed because the e2e test `test_controller_sees_all_services_ready` still expected four services after the runner was added to `/api/services`. I had run the unit/integration suite but not the e2e suite before committing. Test updated. Log: `docs/evidence/phase6_acceptance_linux_run1_e2e_test_not_updated.log`.

Phase 8 pilot (Linux, 1 run per condition): all 8 scenarios completed with valid records, but D1 "none" showed client timeouts at exactly 5 s although the scenario allows 15 s. Cause: the runner's httpx client default timeout (5 s) applied instead of the scenario's `request_timeout_s`. Fixed in `loadgen.py` (per-request timeout); a regression test checks the timeout sent with each request and fails on the old code. D_policies_hang lengthened to a 40 s window so recovery after the breaker's 5 s reset is observable. Re-pilot of D1: "none" bounded at about 10.2 s (outer deadline), timeout ≈ 0.81 s, timeout+retry ≈ 2.5 s, breaker fails fast (≈ 9 ms) with fewer inventory attempts. These are pilot observations, not findings.

Race fixed during Phase 5: the runner briefly reported `completed` before `summary.json` existed (1 failure in about 9 suite runs). The status is now `analysing` until the summary is written; a regression test covers this.

Second defect found by the Phase 4 regression run (C7 failed once, then passed on a re-run). The e2e smoke experiment ends with fallback off. The runner reset the controller afterwards, but the gateway kept that placement until its next 2 s poll, so C7's orders, sent within 2 s, got 503s. Fix: the runner's final cleanup (`ControlPlane.restore`) now also syncs the gateway immediately and checks the version. The integration test runs with a 60 s poll interval and asserts the gateway is back on the file placement as soon as the experiment ends; it fails without the fix. Log of the failing run: `docs/evidence/phase4_regression_on_phase5_linux_run1_stale_gateway.log`.

## Engineering findings from Phases 2–7 (inputs to the threats to validity in `docs/findings.md`)

1. **Platform-dependent failure signatures.** For the same stopped container: Linux Engine 29.9.2 gave an immediate refusal (0.03 s), while Docker Desktop 29.4.2 on Windows gave a 0.3 s connect timeout. With the controller stopped, Linux reported a DNS failure and Windows a `ConnectTimeout`. Failure-detection time is platform-dependent.
2. Runtime placement and link changes live in controller memory; a controller restart reverts to the files.
3. **Recovery time depends on traffic.** A breaker only leaves OPEN when a request arrives after the reset timeout. Acceptance C11 measured 0.021 s from fault expiry to the first inventory-a-served order only because no traffic was sent while the breaker was open. Phase 5 must measure recovery under continuous load and report the probe timing explicitly.
4. **Observation, not a result:** with p = 0.5 seeded errors and retries = 2, 34/40 orders succeeded (85%). Under independence the expectation is 87.5%. H5 is evaluated properly in Phase 8 with repeated runs.
5. Faults are application-level: health endpoints stay up during `error`/`slow`/`hang`, so Docker health status does not reflect them. Container stop covers process failure.
6. **Clock domains.**
   - Windows Phase 4 run 1 (commit cbaee22) failed check C10. The hung call was cut by the 800 ms timeout and the 503 said "timeout", but the client stopwatch on the Windows host read 0.797 s.
   - On Docker Desktop, the host and the Linux VM running the containers are different clock domains, so client wall time cannot serve as a lower bound for a server-side timer.
   - Fix (commit after cbaee22): C10's lower bound now uses the gateway-measured `elapsed_ms` (same clock as the timer); the client time is used only for the upper bound. Evidence: `docs/evidence/phase4_acceptance_windows_run1_c10_check_bug.log`.
   - Implication for Phase 5: latency must be measured by a generator in the same clock domain as the services (inside the Compose network). Any cross-domain timing must be labelled as such.

7. **Recovery-time window lag.** `recovery_s` uses a 1 s look-back window, so an instantaneous recovery reads as about 0.9–1.0 s at 10 req/s (the smoke runs showed 0.9 and 1.1 s). This is a property of the definition, not of the system; it must be stated wherever recovery times are reported.
8. **Scheduling precision.** In the Linux workspace the generator's send lag stayed below about 7 ms at 10 req/s, and faults were injected less than 1 ms late. Both are recorded per run. On Windows these must be read from the records rather than assumed.

## Unresolved issues and known limitations (final)

1. **Real clouds are not implemented.** The AWS/Azure/GCP adapters validate configuration only; no provider was measured. This is by design and stated everywhere (`docs/cloud_setup.md`).
2. **No admission control or load shedding** in the gateway: above ≈ 40 req/s on Parv's PC, goodput collapses (findings §E). Documented, not fixed.
3. **Statistical power is modest:** 3 runs per condition (5 for A); p99 reportable only for A. Conclusions rely on effects much larger than run-to-run spread.
4. **Screenshots** were captured in the Linux build workspace (showing the Windows records). Replacement instructions are in `docs/evidence/screenshots/phase10/README.md`.
5. **npm audit** reports a high-severity advisory in `image-size`, a dependency of pptxgenjs 4.0.1 (denial of service on crafted images). The generator reads only this repository's own PNGs; there is no fix without a breaking upgrade, so it is recorded rather than forced.
6. Phase 2 acceptance scripts are a snapshot: A9/A10 assume no fallback and now get 200s (documented).
7. Experiments started from the dashboard record the git commit as "not recorded" (the container has no git). Use `scripts/run_all_experiments.ps1` or `run_experiment.ps1` for traceable runs.
8. Leftover folders on Parv's PC: `E:\Projects\Cloud_Mini_Project\.p0\` (sync archives) and `_to_delete\` are safe to delete.
9. Name: `setu-x` was free on GitHub under Parv-01. That is not a trademark check.

## Build-workspace notes (not part of the repo)

- Start the daemon if needed (it does not survive between turns): `nohup dockerd > <scratchpad>/dockerd.log 2>&1 &`
- Images: `<scratchpad>/sandbox_build.sh` (tags `setu-x:0.5.0`, `setu-x-test:0.5.0`). Acceptance: `pwsh scripts/phase5_acceptance.ps1 -BuildCommand "bash <scratchpad>/sandbox_build.sh"`.
- Local test run without Docker: `pip install -e .[dev] -c requirements/dev.lock`, then `python -m pytest -m 'not e2e'`.
- PowerShell 7.6.8: `<scratchpad>/pwsh/pwsh`, symlinked to `/usr/local/bin/pwsh` (needed because the acceptance script calls `pwsh` for `fault_container.ps1`).
- Sync: **use a new archive file name for every sync** (reusing a name once delivered a stale copy despite reporting success) and compare `sha256sum` on both sides. Then tar → `device_commit_files` to `.p0\setu-x-<phase>.tgz` → on the device: `tar --overwrite --exclude='setu-x/.git/objects/*'`, then `tar --skip-old-files … 'setu-x/.git/objects'`, then `git fsck`.

## Possible next steps (beyond the brief)

1. Real-provider runs of B, F and G with the adapters' targets (ECS Fargate, Azure Container Apps, Cloud Run); needs credentials and a cost cap (`docs/cloud_setup.md`).
2. Jitter and packet-loss models for links; compare against measured provider round-trip times.
3. Admission control in the gateway, then repeat Experiment E.
4. More runs per condition (≥ 10) to tighten the intervals and make p99 reportable.
