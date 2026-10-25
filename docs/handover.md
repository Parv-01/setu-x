# SETU-X Handover

Resume from this file if work is interrupted. Updated at every phase boundary.

**Last updated:** 2026-10-25 (end of Phase 2)
**Current phase:** Phase 2 complete (PASS in the Linux build workspace; Windows run pending). Next: Phase 3, placement and provider abstraction.

## Completed

- **Phase 0 (PARTIAL):** environment inventory and plan. Windows Docker client and Compose are present, but the engine was not running when Parv tested.
- **Phase 1 (PASS):** research question, H1–H5, literature review and matrix, verified bibliography (28/28 DOIs), architecture with diagrams, Windows testing guide, doc integrity checker.
- **Phase 2 (PASS, Linux build workspace):**
  - Package `src/setu_x`: `common` (config validation, request/trace IDs, JSON logging, app factory with error envelope and live/ready endpoints), `domain.py` (deterministic stock and prices), and services `inventory`, `pricing`, `gateway` (parallel/sequential calls, per-call timeout, outer deadline, degraded pricing fallback, `/health/dependencies`).
  - Packaging: `pyproject.toml` with exact pins; `requirements/runtime.lock` and `dev.lock`.
  - Containers: a multi-stage `Dockerfile` (runtime and test targets, non-root, base image pinned by digest); `compose.yaml` (inventory-a, pricing, gateway, plus a `tests` profile; health checks, `depends_on: service_healthy`, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, 0.5 CPU / 256 MiB, port bound to 127.0.0.1).
  - Supporting files: `.env.example`, `Makefile`, `.vscode/` (settings and extension recommendations).
  - Tests: 74 unit + integration tests; 4 e2e tests.
  - Acceptance scripts: `scripts/phase2_acceptance.sh` and `scripts/phase2_acceptance.ps1`.
  - Evidence: `docs/evidence/phase2_acceptance.log` (bash), `docs/evidence/phase2_acceptance_linux.log` (PowerShell 7.6.8 on Linux), a sample order and a gateway log sample.

## Test status (executed in the build workspace)

| Check | Result |
|---|---|
| Unit + integration tests (Python 3.11.17 in test image) | 74 passed |
| e2e tests against running stack | 4 passed |
| Mutation check: timeout logic disabled | 4 timeout/deadline tests failed as they should |
| `ruff check` | clean |
| `phase2_acceptance.sh` | 14/14 PASS |
| `phase2_acceptance.ps1` under PowerShell 7.6.8 (Linux) | 14/14 PASS |
| Negative run: invalid config (timeout > deadline) | gate FAILS (11 checks fail, exit 11); gateway exits 2 with a clear message |
| Windows (Parv's PC) | **not yet run**: needs Docker Desktop running, then `pwsh -ExecutionPolicy Bypass -File scripts\phase2_acceptance.ps1` |

Measured during acceptance (build workspace; illustrative, not experiment results):
- Stopped inventory → 503 in about 0.03 s (connect error).
- Paused inventory → 503 in about 0.81 s (per-attempt timeout 0.8 s).
- Stopped pricing → degraded order in about 0.03 s.

## Unresolved issues

1. Windows Docker engine not yet confirmed running; Windows acceptance log pending.
2. GitHub credentials unavailable; GitHub name availability unverified.
3. No cloud credentials (expected).
4. Leftover folder `E:\Projects\Cloud_Mini_Project\.p0\` (sync staging; safe to delete).
5. License not yet chosen (Phase 9).

## Build-workspace notes (not part of the repo)

- Start the daemon if needed: `nohup dockerd > <scratchpad>/dockerd.log 2>&1 &`
- Compose v5 builds through Bake, which refuses host networking without `--allow`. In this workspace, images are built with `<scratchpad>/sandbox_build.sh` (`docker build --network host` + proxy args + `pip_ca` secret, same Dockerfile targets), and acceptance runs with `SETU_BUILD_CMD=<that script>`. The committed `compose.yaml` is used unchanged.
- PowerShell 7.6.8 for Linux is unpacked at `<scratchpad>/pwsh/pwsh` for testing `.ps1` scripts.
- Sync: tar → `device_commit_files` to `.p0\setu-x-sync.tgz` → on the device: `tar --overwrite --exclude='setu-x/.git/objects/*' -xzf …` then `tar --skip-old-files -xzf … 'setu-x/.git/objects'`, then `git fsck`.

## Next commands (Phase 3)

1. `configs/environments.yaml` and `configs/placement.yaml` with Pydantic validation; add `inventory-b` (gcp-simulated) and a minimal `controller` service (placement API, health aggregation).
2. `src/setu_x/adapters/`: `base.py` (CloudAdapter protocol, Operation/OperationSupport, status ladder), `local_docker.py`, and `aws.py`, `azure.py`, `gcp.py` skeletons that report `not_implemented` honestly.
3. Routing policies (primary-fallback, round-robin, weighted) with observable decisions; link-delay injection in simulated mode.
4. Tests: adapter contract suite, config validation per rule, routing policy determinism, simulated labels in every response.
