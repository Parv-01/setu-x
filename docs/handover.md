# SETU-X Handover

Resume from this file if work is interrupted. Updated at every phase boundary.

**Last updated:** 2026-10-25 (end of Phase 3)
**Current phase:** Phase 3 complete (PASS in the Linux build workspace; Windows run pending). Next: Phase 4, resilience and failure injection.

## Completed

- **Phase 0 (PASS):** environment inventory and plan. Windows Docker confirmed by Parv's Phase 2 run (Docker 29.4.2, Compose 5.1.3).
- **Phase 1 (PASS):** research question, H1–H5, literature review and matrix, verified bibliography (28/28 DOIs), architecture, doc integrity checker.
- **Phase 2 (PASS on Linux and Windows):** gateway, inventory, pricing; hardened containers; 14-check acceptance. Windows log: `docs/evidence/phase2_acceptance_windows.log` (commit aa1aab0, 14/14).
- **Phase 3 (PASS, Linux build workspace):**
  - Configuration:
    - `configs/environments.yaml`: local plus three `*-simulated` labels, and a complete illustrative RTT matrix.
    - `configs/placement.yaml`: instances, routing and resilience.
    - `setu_x/placement.py`: Pydantic validation with file and field-path errors.
    - `python -m setu_x.config_check`: validates a config directory.
  - Adapters (`setu_x/adapters/`):
    - `base.py`: Operation / OperationSupport / AdapterStatus; evidence-gated statuses; cloud-mode + confirm guards.
    - `local_docker.py`: implemented over HTTP.
    - `cloud.py`: AWS/Azure/GCP config-only skeletons.
  - Routing (`setu_x/routing.py`): primary-fallback, round-robin, smooth weighted round-robin; bounded decision log with reasons.
  - Controller service: environments, adapters, placement GET/PUT/reset (validated; names and URLs locked to the file), services health.
  - Gateway: placement-aware routing; polls the controller and falls back to the last valid placement; `/api/routing/decisions`, `/api/placement/current`; responses list `simulated_environments` plus a notice.
  - Compose: adds `inventory-b` (gcp-simulated) and `controller` (127.0.0.1:8090); `configs/` mounted read-only; version 0.3.0.
  - Tests: 189 unit/contract/integration + 7 e2e. Acceptance: `scripts/phase3_acceptance.ps1` (15 checks) with shared helpers in `scripts/acceptance/common.ps1`.
  - Docs: `docs/cloud_setup.md` (status, targets, safety rails, planned operations, smoke test); architecture and research updates.

## Test status (executed in the build workspace)

| Check | Result |
|---|---|
| Unit + contract + integration (Python 3.11.17) | 189 passed |
| e2e against running 5-service stack | 7 passed |
| Mutation checks (round-robin, evidence gate, cloud-mode guard, PUT URL lock, simulated-suffix rule) | each mutant caught (1–4 failing tests each) |
| `ruff check` | clean |
| `phase3_acceptance.ps1` (PowerShell 7.6.8, Linux) | 15/15 PASS |
| `phase2_acceptance.ps1` as regression on the Phase 3 stack | 14/14 PASS |
| Windows (Parv's PC) | Phase 3 run pending |

## Findings to carry into Phase 8 (threats to validity / limitations)

1. **Platform-dependent failure signatures.**
   - Linux Docker Engine 29.9.2: a stopped container gave an immediate connection refusal (0.03 s).
   - Docker Desktop 29.4.2 on Windows: the same case was a 0.3 s connect **timeout**.
   - Same images and config, so failure detection time depends on the platform. This matters for recovery-time measurements.
2. Runtime placement changes live in controller memory. A controller restart reverts to `placement.yaml`.

## Unresolved issues

1. Windows Phase 3 acceptance log pending.
2. GitHub credentials unavailable; GitHub name availability unverified.
3. No cloud credentials (expected); real-cloud integration not implemented (documented).
4. Leftover folders `E:\Projects\Cloud_Mini_Project\.p0\` (sync staging) and `_to_delete\` (safe to delete).
5. License not yet chosen (Phase 9).

## Build-workspace notes (not part of the repo)

- Start the daemon if needed (it does not survive between turns): `nohup dockerd > <scratchpad>/dockerd.log 2>&1 &`
- Images: `<scratchpad>/sandbox_build.sh` (`docker build --network host` + proxy args + `pip_ca` secret; tags `setu-x:0.3.0`, `setu-x-test:0.3.0`).
- PowerShell 7.6.8 for Linux: `<scratchpad>/pwsh/pwsh`. Acceptance: `pwsh -File scripts/phase3_acceptance.ps1 -BuildCommand <sandbox_build.sh>`.
- Sync: tar → `device_commit_files` to `.p0\setu-x-sync.tgz` → on the device: `tar --overwrite --exclude='setu-x/.git/objects/*'` then `tar --skip-old-files … 'setu-x/.git/objects'`, then `git fsck`.

## Next commands (Phase 4)

1. Link-delay injection in the gateway client for `SETU_MODE=simulated` (RTT/2 before send and after receive), exported as `injected_delay_ms`.
2. Bounded fault injection on inventory/pricing (`error`, `slow`, `hang`; allow-list; mandatory duration ≤ 300 s; auto-expire), controlled via the controller; `scripts/fault_container.ps1` for stop/start.
3. Retries with full-jitter backoff (cap 3, clipped to the deadline), a circuit breaker per downstream instance, and fallback over the routing candidate list; resilience events log.
4. Tests: bounds, breaker transitions, retry amplification ≤ 1 + r, fallback correctness, recovery observable. Note: Phase 2 acceptance A9 expects a 503 when inventory-a stops; with fallback enabled this becomes a 200 from inventory-b, so A9 will be superseded by a Phase 4 check.
