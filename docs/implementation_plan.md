# SETU-X Implementation Plan (Phase 0)

**Project:** SETU-X (Service portability, Experimentation, Testing, Unified orchestration, X = cross-environment execution and evaluation). "Setu" is Sanskrit for *bridge*; the expansion is a project-specific backronym, not a Sanskrit phrase.
**Author:** Parv Agarwal, Junior Research Associate (Technical), AI NLP ML Research Laboratory, IIT Patna. This is an independent mini-project, not an officially endorsed IIT Patna project.
**Repository (provisional):** `setu-x` · **Python package:** `setu_x`
**Plan date:** 2026-10-25 · **Status of Phase 0:** see section 7.

---

## 1. Environment inventory (checked, not assumed)

Three execution contexts are involved. Only the first one can run Docker today.

### 1a. Build-and-test workspace (Claude's cloud Linux container)

| Tool | Version found | Status |
|---|---|---|
| OS | Ubuntu 24.04.5 LTS, kernel 6.18.44 | ok |
| Docker Engine (client and server) | 29.9.2 (daemon started manually with `dockerd`, overlayfs, cgroup v1) | **works**: `hello-world` ran; `python:3.11-slim` pulled and ran Python 3.11.17 |
| Docker Compose | v5.6.1 | installed |
| Docker Buildx | v0.38.1 | installed |
| Container-to-container HTTP on a user-defined bridge network | — | **works** (HTTP 200 between two containers) |
| Python (host) | 3.13.17 | works; containers will use 3.11 |
| Node / npm | 22.23.4 / 10.9.9 | works |
| Git | 2.43.0 | works |
| LibreOffice (headless) | 24.2.7.2 | **works**: converted a test `.pptx` to PDF |
| pdftoppm (poppler) | 24.02.0 | **works**: rendered the PDF to PNG |
| python-pptx | 1.0.2 | **works**: wrote and reopened a test deck, slide count read back |
| matplotlib | 3.11.2 | installed |
| Playwright (Node) + Chromium | 1.63.0 | **works**: took a headless screenshot |
| FastAPI, httpx, pytest | — | **missing on host**; installable from PyPI (download test succeeded) |
| Resources | 2 vCPU, 7.8 GiB RAM | small; limits load-test scale |

**Network quirk found and solved:** containers on the default bridge cannot reach PyPI because outbound traffic must go through the workspace's HTTPS proxy. `pip install` inside a container **succeeded** when the container used `--network host` with the proxy variables and CA bundle passed in. Image builds in this workspace will therefore use a build-time override file kept *outside* the repository. The committed `compose.yaml` stays standard and does not need it on a normal machine.

### 1b. Parv's Windows computer (`parv-agarwal`, win32 x64)

| Item | Finding | Status |
|---|---|---|
| Project folder `E:\Projects\Cloud_Mini_Project` | Empty, not a Git repository | ok, writable |
| Docker Desktop | `C:\Program Files\Docker\Docker` exists | installed |
| Docker client / Compose (reported by Parv, 2026-10-25, PowerShell 7.6.8) | client 29.4.2 (context `desktop-linux`), Compose v5.1.3 | installed. **Engine NOT running**: `docker version` and `docker run hello-world` failed with `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`. Fix steps in `docs/testing_on_windows.md` §7. The Compose file will avoid features newer than v5.1.3 |
| File deletion in the project folder | blocked by default (`rm` → "Operation not permitted") | Syncing uses `tar --overwrite`, which was tested and works |

### 1c. Linux helper VM on Parv's computer (used only to write files into the folder)

Ubuntu 22.04.5, Python 3.10.12, Git 2.34.1, Node 22.23.4, LibreOffice 26.2.6.3, python-pptx 1.0.2, matplotlib 3.10.9. **No Docker, no Podman.** Reaches PyPI and GitHub. Not used for running the stack.

### 1d. External services

| Dependency | Needs internet | Needs credentials | Notes |
|---|---|---|---|
| PyPI packages | yes (build time) | no | reachable |
| Docker Hub base images | yes (build time) | no | reachable from the workspace |
| GitHub push / repo creation | yes | **yes, not available** | The session's GitHub token is invalid and GitHub search is blocked from here. Nothing will be pushed without your explicit go-ahead and credentials |
| AWS / Azure / GCP | yes | **yes, not available** | Real-cloud mode stays off by default. No adapter is marked above `simulated`/`implemented` without a real smoke test |
| Literature (DOIs, publisher pages) | yes | no | Web search, DOI resolver, alphaXiv and a citation verifier are available |

**Name check (partial):** PyPI returns 404 for both `setu-x` and `setu_x` (no package by those names today). GitHub repository search is blocked from this session, so GitHub name availability is **unverified**. A web search found an unrelated project named `setu` (`setucp/setu`), but no `setu-x` repository. This is not a trademark check.

---

## 2. Key design decisions (with reasons)

1. **Docker-first, local-first.** The first release runs with `docker compose up --build -d` and needs no cloud credentials, paid cluster, monitoring service or cloud database.
2. **Delay and failure injection at the application layer, not with `tc netem`.** `netem` needs `NET_ADMIN`, and the brief forbids privileged containers in the normal demo. Each outbound call goes through a client wrapper that adds the configured delay for the (source environment → destination environment) link before sending. Consequence (a threat to validity): injected delay is a *model* of WAN latency (no jitter distribution, packet loss or bandwidth limits unless explicitly modelled). It will be labelled "injected" in every export, separately from measured time.
3. **Smallest service set that can test the hypotheses.**

   | Container | Role | Environment label (simulated mode) |
   |---|---|---|
   | `gateway` | Public entry, request IDs, validation, orchestration of the order flow, resilience policies (timeout, bounded retry with jitter, circuit breaker, fallback), end-to-end timing | `local` |
   | `inventory-a` | Deterministic stock computation, injectable delay/failure | `aws-simulated` |
   | `inventory-b` | Second replica of inventory, for fallback/failure-domain experiments | `gcp-simulated` |
   | `pricing` | Deterministic price computation, supports degraded responses | `azure-simulated` |
   | `controller` | Placement and routing config, bounded fault-injection control plane, experiment runner, metrics/export, serves the dashboard | `local` |

   The order logic lives in the gateway (combining "gateway" and "order service") because a separate order hop would add a dependency without testing anything new. This can be split later if Experiment F needs a deeper call graph.
4. **Provider adapters behind one interface** (`CloudAdapter`) with explicit per-capability status: `simulated`, `configured`, `connected`, `deployed`, `verified`. Plus `not_implemented`/`unsupported` per operation. The local adapter does health and endpoint discovery over HTTP and does **not** mount the Docker socket (that would grant host-level control).
5. **Three modes** via `SETU_MODE`: `local` (default), `simulated` (environment labels and link delays active), `cloud` (disabled unless explicitly enabled and confirmed).
6. **Measurements:** raw per-request observations (JSON Lines) are kept for every run. Percentiles are computed from raw samples, never by averaging per-run percentiles. The unit of replication is the *run*, not the request.
7. **Dashboard:** static HTML/JS served by the controller. The charting library is vendored into the repo (pinned version, license kept) so the demo works offline.
8. **Testing:** pytest with `unit/`, `contract/` (adapter interface), `integration/` (in-process ASGI apps with stubbed downstreams), `end_to_end/` (against a running Compose stack). Tests run inside a `python:3.11-slim` container so the workspace's Python 3.13 does not hide version issues. No test needs cloud credentials.
9. **Screenshots** are genuine captures from the stack running in the build workspace (Playwright for the dashboard). Terminal evidence such as `docker compose ps` is saved as raw text and shown as captured output. Every screenshot caption says it was taken in the Linux build workspace, not on your Windows machine, unless you capture replacements yourself (instructions will be provided).
10. **Presentation:** at most 5 slides (asserted in code), built with python-pptx, rendered through LibreOffice to PDF/PNG and visually inspected. Talk length 5–7 minutes, so the voiceover targets about 700–950 spoken words.

---

## 3. Milestones and acceptance gates

Each phase ends with executed tests and a PASS / PARTIAL / BLOCKED report. `docs/handover.md` is updated at every phase boundary.

| Phase | Deliverable | Acceptance criteria (must be shown with real output) |
|---|---|---|
| 0 Environment | this file, `docs/handover.md`, `scripts/check_env.sh` | Tool versions recorded; Python and Docker runs verified; PPTX pipeline verified; gaps listed honestly |
| 1 Research and architecture | `docs/architecture.md`, `research_questions.md`, `literature_review.md`, `references.bib`, literature matrix | Seed references checked against DOI/publisher pages; every claim cited or marked as a hypothesis; every experiment runnable on the planned services |
| 2 Minimal services | services, Dockerfiles, `compose.yaml`, Makefile targets | `docker compose config` valid; all images build; health endpoints answer; a gateway request returns the expected deterministic result; a stopped downstream gives a bounded error (no hang); clean down/up cycle |
| 3 Placement and adapters | `adapters/`, `configs/`, routing | Adapter contract tests pass for all four adapters; simulated environments labelled everywhere; invalid config rejected with a clear message; routing follows the policy; no credentials needed |
| 4 Resilience and injection | injection API, timeouts, retries, breaker, fallback | Injection limited to an allow-list and bounded duration; requests finish within documented bounds; retries capped; breaker transitions match the spec; fallback and recovery tested |
| 5 Benchmarking | runner, metrics, statistics, export | Short baseline completes; raw rows reconcile with summary counts; percentiles verified against a reference calculation; unique experiment IDs; invalid records flagged |
| 6 Dashboard | `dashboard/` | Controls call the right endpoints (tested); empty state renders; simulated vs measured clearly marked; API errors shown |
| 7 End-to-end validation | `docs/validation_report.md` | Clean checkout → up → full test suite → experiment → export → down, all with captured output; README commands tested; no secrets |
| 8 Experiments and findings | `experimental_results.md`, `limitations.md`, `threats_to_validity.md`, plots | Experiments A–G run with repeats; each plot traced to a results folder; hypotheses judged on measured data, including contrary results |
| 9 GitHub readiness | README, CI, license, security notes | CI workflow runs the documented checks (executed locally with the same commands); secret scan clean; no invented repository URL |
| 10 Presentation | `presentation/setu_x.pptx` + generator | ≤ 5 slides asserted programmatically; real chart(s) and genuine screenshot(s) included; rendered and inspected |
| 11 Voiceover and handover | `presentation/voiceover.md`, final handover | Matches the 5 slides exactly; word count fits 5–7 minutes; final handover complete |

---

## 4. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Docker on your Windows machine is unverified | The "runs on your own computer" requirement cannot be marked PASS from here | Everything is tested in the Linux workspace. You run `scripts/check_env` steps and `docker compose up --build -d` on Windows. Windows-specific issues (line endings, paths) are guarded by `.gitattributes` (LF for shell scripts and Dockerfiles) |
| Application-level delay is a simplification | Results describe the model, not real WAN behaviour | Labelled "injected" everywhere; discussed in threats to validity; real-cloud path documented |
| 2-vCPU workspace | Saturation results reflect the sandbox only | Workload-scaling claims are limited to "this host"; no cloud-scale extrapolation |
| Load generator shares the host with the system under test | Measurement interference | Load generator rate is bounded and logged; the warm-up period is discarded; this limitation is reported |
| Screenshots come from the build workspace | Not proof of execution on your PC | Captions say so; capture instructions provided so you can replace them |
| No GitHub credentials | Cannot publish | Repo prepared locally with Git history; publication steps documented; nothing pushed without your authorisation |
| No cloud credentials | Real-cloud adapters stay at `implemented` at most | Status labels enforced in code and tests |
| Paywalled papers | Cannot read full text of some references | Only claims supported by what was actually read (abstract or full text) are used; the matrix records which |

---

## 5. How files reach your folder

The source of truth during development is `/home/claude/setu-x` in the build workspace (with Git history). At the end of each phase the tree is archived and extracted into `E:\Projects\Cloud_Mini_Project\setu-x\` with `tar --overwrite`. Because deletes are blocked in your folder, files removed in the workspace will not disappear from your copy automatically. If that happens, I will list them for you, or ask for delete permission.

Leftover from Phase 0 testing: an empty test folder `E:\Projects\Cloud_Mini_Project\.p0\` containing `a.txt` (the delete test was refused, as expected). Safe to delete.

---

## 6. Commands you can run on Windows to close the Docker gap

In PowerShell, with Docker Desktop started:

```powershell
docker version
docker compose version
docker run --rm hello-world
```

Paste the output back to me, or tell me whether it worked. Once Phase 2 is synced, the stack check is:

```powershell
cd E:\Projects\Cloud_Mini_Project\setu-x
docker compose config
docker compose up --build -d
docker compose ps
```

---

## 7. Phase 0 status: PARTIAL

| Acceptance criterion | Result |
|---|---|
| Actual tool versions recorded | PASS (section 1) |
| Python can run a minimal program | PASS: host Python 3.13.17, container Python 3.11.17, helper VM Python 3.10.12 |
| Docker availability verified | PASS in the build workspace (Engine 29.9.2, Compose 5.6.1; containers run and talk to each other). **UNVERIFIED on your Windows machine** |
| PowerPoint generation and validation path identified | PASS: python-pptx 1.0.2 → LibreOffice 24.2 → PDF → pdftoppm PNG, all executed |
| Missing capabilities not reported as passing | Yes: Windows Docker, GitHub credentials, cloud credentials and GitHub name availability are all listed as unverified/unavailable |

Phase 0 is PARTIAL only because of the Windows Docker check, which needs you. It does **not** block Phase 1 or development, so work continues.

**Update 2026-10-25:** Parv's output confirms the Docker client and Compose are installed on Windows, but the engine was not running. Phase 0 stays PARTIAL until `docker run --rm hello-world` succeeds there.

**Design change from Phase 1 research:** AWS App Runner was dropped as the AWS target because it is no longer open to new customers. The AWS target is now Amazon ECS Express Mode on Fargate (see `docs/architecture.md` §6.3).
