# SETU-X Handover

Resume from this file if work is interrupted. Updated at every phase boundary.

**Last updated:** 2026-10-25 (end of Phase 4)
**Current phase:** Phase 4 complete (PASS in the Linux build workspace; Windows run pending). Next: Phase 5, benchmarking and data collection.

## Completed

- **Phase 0 (PASS):** environment inventory and plan; Windows Docker confirmed (29.4.2, Compose 5.1.3).
- **Phase 1 (PASS):** RQ, H1–H5, literature review and matrix, verified bibliography, architecture.
- **Phase 2 (PASS, Linux + Windows):** gateway, inventory, pricing; hardened containers. Windows log `docs/evidence/phase2_acceptance_windows.log` (14/14).
- **Phase 3 (PASS, Linux + Windows):** placement and environment config with validation; adapter contract (local + AWS/Azure/GCP config-only skeletons); routing policies; controller. Windows log `docs/evidence/phase3_acceptance_windows.log` (15/15, commit c5b9121).
- **Phase 4 (PASS, Linux build workspace):**
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

## Test status (executed in the build workspace)

| Check | Result |
|---|---|
| Unit + contract + integration (Python 3.11.17) | 236 passed (3 repeated runs, no flakes) |
| e2e against running stack | 8 passed |
| Mutation checks (retry cap, breaker never opens, fallback ignored, multi-probe half-open, delay in local mode, unbounded fault duration, allow-list bypass, backoff ignoring deadline, 4xx retried) | all 9 caught. The deadline one initially survived, so a test was added |
| `ruff check` | clean |
| `phase4_acceptance.ps1` (pwsh 7.6.8, Linux) | 15/15 PASS |
| Windows (Parv's PC) | Phase 4 run pending |

## Findings to carry into Phase 8 (threats to validity / limitations)

1. **Platform-dependent failure signatures.** For the same stopped container: Linux Engine 29.9.2 gave an immediate refusal (0.03 s), while Docker Desktop 29.4.2 on Windows gave a 0.3 s connect timeout. With the controller stopped, Linux reported a DNS failure and Windows a `ConnectTimeout`. Failure-detection time is platform-dependent.
2. Runtime placement and link changes live in controller memory; a controller restart reverts to the files.
3. **Recovery time depends on traffic.** A breaker only leaves OPEN when a request arrives after the reset timeout. Acceptance C11 measured 0.021 s from fault expiry to the first inventory-a-served order only because no traffic was sent while the breaker was open. Phase 5 must measure recovery under continuous load and report the probe timing explicitly.
4. **Observation, not a result:** with p = 0.5 seeded errors and retries = 2, 34/40 orders succeeded (85%). Under independence the expectation is 87.5%. H5 is evaluated properly in Phase 8 with repeated runs.
5. Faults are application-level: health endpoints stay up during `error`/`slow`/`hang`, so Docker health status does not reflect them. Container stop covers process failure.

## Unresolved issues

1. Windows Phase 4 acceptance log pending.
2. GitHub credentials unavailable; GitHub name availability unverified.
3. No cloud credentials (expected); real-cloud integration not implemented (documented).
4. Leftover folders `E:\Projects\Cloud_Mini_Project\.p0\` (sync staging) and `_to_delete\` (safe to delete).
5. License not yet chosen (Phase 9).
6. Phase 2 acceptance scripts are a snapshot: A9/A10 assume no fallback and now get 200s (documented).

## Build-workspace notes (not part of the repo)

- Start the daemon if needed (it does not survive between turns): `nohup dockerd > <scratchpad>/dockerd.log 2>&1 &`
- Images: `<scratchpad>/sandbox_build.sh` (tags `setu-x:0.4.0`, `setu-x-test:0.4.0`).
- PowerShell 7.6.8: `<scratchpad>/pwsh/pwsh`, symlinked to `/usr/local/bin/pwsh` (needed because the acceptance script calls `pwsh` for `fault_container.ps1`).
- Sync: **use a new archive file name for every sync** (reusing a name once delivered a stale copy despite reporting success) and compare `sha256sum` on both sides. Then tar → `device_commit_files` to `.p0\setu-x-<phase>.tgz` → on the device: `tar --overwrite --exclude='setu-x/.git/objects/*'`, then `tar --skip-old-files … 'setu-x/.git/objects'`, then `git fsck`.

## Next commands (Phase 5)

1. Experiment runner (controller-hosted, plus a CLI): open-loop load at a fixed rate with an in-flight cap; warm-up and measurement phases; repeated runs; seeded.
2. Scenario files `configs/scenarios/A_baseline.yaml` … `G_hybrid_fallback.yaml`. Each sets placement/resilience/links via the controller APIs and faults via `/api/faults`. Container-stop scenarios are driven from the host.
3. Records: `results/<id>/manifest.json`, `observations.jsonl` + `.csv`, `events.jsonl`, `summary.json`. Validation: counts reconcile; percentiles from raw rows; per-run vs pooled.
4. Recovery time under continuous load (rolling 1 s success rate back to ≥ 95% of baseline for 3 s).
5. `docs/methodology.md`; tests for metrics, export validation and ID uniqueness.
