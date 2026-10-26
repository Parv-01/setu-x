# SETU-X

[![ci](https://github.com/Parv-01/setu-x/actions/workflows/ci.yml/badge.svg)](https://github.com/Parv-01/setu-x/actions/workflows/ci.yml) · Repository: <https://github.com/Parv-01/setu-x> · License: Apache-2.0 (code), CC BY 4.0 (docs and results)

**S**ervice portability · **E**xperimentation · **T**esting · **U**nified orchestration · **X** cross-environment execution and evaluation

*Setu* is Sanskrit for "bridge". The expansion is a project-specific backronym, not a Sanskrit phrase.

SETU-X is a local-first experimental platform. It shows how a small containerized microservice application behaves when its services are placed across heterogeneous environments: local, plus **simulated** AWS, Azure and GCP. It also shows how network delay, failures and resilience policies change latency and availability.

> **Simulation notice.** Labels such as `aws-simulated` are logical environments on one Docker host. They are **not** deployments on AWS, Azure or Google Cloud, and results from them are not measurements of those providers.

**Author:** Parv Agarwal, Junior Research Associate (Technical), AI NLP ML Research Laboratory, IIT Patna. Independent mini-project, not an officially endorsed IIT Patna project.

**Status:** complete (all phases, 0–10). Experiment results from one Windows PC are in [`results/`](results/) and the analysis is in [`docs/findings.md`](docs/findings.md); they describe that machine only. Final five-slide deck: [`presentation/setu_x.pptx`](presentation/setu_x.pptx) ([PDF render](presentation/setu_x.pdf), [voiceover](presentation/voiceover.md)). Dashboard at http://127.0.0.1:8090/. Six services: gateway, controller, inventory-a, inventory-b, pricing, runner. Real AWS/Azure/GCP integration is **not implemented** (adapters validate configuration only; see [`docs/cloud_setup.md`](docs/cloud_setup.md)). Handover: [`docs/handover.md`](docs/handover.md).

## Quick start (Docker only)

Requirements: Docker Engine with Docker Compose v5.1.3 or later. Docker Desktop on Windows/macOS must show "Engine running".

```bash
docker compose config              # validate the configuration
docker compose up --build -d       # build and start (gateway :8080, controller :8090, both on 127.0.0.1)
docker compose ps                  # all services should be "healthy"
curl -s -X POST http://127.0.0.1:8080/api/orders \
  -H 'content-type: application/json' \
  -d '{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}'
docker compose logs gateway        # structured JSON logs
docker compose down                # stop
```

**Dashboard: http://127.0.0.1:8090/** (guide: [`docs/dashboard.md`](docs/dashboard.md)). Interactive API docs: gateway http://127.0.0.1:8080/docs · controller http://127.0.0.1:8090/docs

Useful endpoints: `GET :8090/api/environments`, `GET :8090/api/adapters`, `GET :8090/api/services`, `GET|PUT :8090/api/placement`, `PUT :8090/api/links`, `POST|GET|DELETE :8090/api/faults`, `GET :8090/api/events`, `GET :8080/api/routing/decisions`, `GET :8080/api/resilience/events`, `GET :8080/api/resilience/breakers`, `POST :8080/api/resilience/reset`, `GET :8090/api/scenarios`, `GET|POST :8090/api/experiments`.

Simulated network delay: start the stack with `SETU_MODE=simulated` (e.g. in `.env`); the gateway then injects the configured link delays and reports them separately as `injected_delay_ms`. Container-level faults: `pwsh -File scripts/fault_container.ps1 -Service inventory-a -Seconds 20`.

Experiments: `pwsh -File scripts/run_experiment.ps1 -Scenario smoke` starts a scenario from [`configs/scenarios/`](configs/scenarios/) through the controller, shows progress and downloads the records into `results/<experiment_id>/`. Method: [`docs/methodology.md`](docs/methodology.md).

Configuration: [`configs/environments.yaml`](configs/environments.yaml) (environments and illustrative link delays) and [`configs/placement.yaml`](configs/placement.yaml) (instances, routing policy, timeouts). Validate with `docker compose --profile test run --rm --no-deps tests python -m setu_x.config_check configs`.

On Windows PowerShell, use `Invoke-RestMethod` (see [`docs/testing_on_windows.md`](docs/testing_on_windows.md)).

## Tests

```bash
docker compose --profile test run --rm tests                 # unit + integration (no running stack needed)
docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e   # end-to-end (stack up)
bash scripts/phase2_acceptance.sh                            # Phase 2 snapshot (A9/A10 predate fallback; see docs/testing_on_windows.md)
pwsh -ExecutionPolicy Bypass -File scripts/phase2_acceptance.ps1   # same checks in PowerShell 7 (Windows)
pwsh -ExecutionPolicy Bypass -File scripts/phase3_acceptance.ps1   # Phase 3: placement, routing, adapters (any OS with pwsh 7)
pwsh -ExecutionPolicy Bypass -File scripts/phase4_acceptance.ps1   # Phase 4: injection, retries, breaker, fallback, simulated delay
pwsh -ExecutionPolicy Bypass -File scripts/phase5_acceptance.ps1   # Phase 5: experiment runner, records, validation, statistics
pwsh -ExecutionPolicy Bypass -File scripts/phase6_acceptance.ps1   # Phase 6: dashboard, incl. real-browser tests (local + simulated)
docker compose --profile browser run --rm browser-tests             # dashboard browser tests only (stack up; ~3.5 GB image)
node --test dashboard/tests/*.test.mjs                               # dashboard JS helpers (Node 18+, optional)
pwsh -ExecutionPolicy Bypass -File scripts/verify.ps1            # Phase 7: everything above from a clean checkout (report: docs/validation_report.md)
```

## Documentation

- Research question and hypotheses: [`docs/research_questions.md`](docs/research_questions.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Measurement and statistical method: [`docs/methodology.md`](docs/methodology.md)
- Dashboard guide: [`docs/dashboard.md`](docs/dashboard.md)
- Literature review: [`docs/literature_review.md`](docs/literature_review.md), bibliography [`docs/references.bib`](docs/references.bib)
- Implementation plan: [`docs/implementation_plan.md`](docs/implementation_plan.md)
- Real-cloud status and plan: [`docs/cloud_setup.md`](docs/cloud_setup.md)
- Experimental findings: [`docs/findings.md`](docs/findings.md)
- CI, security and release: [`.github/workflows/ci.yml`](.github/workflows/ci.yml), [`SECURITY.md`](SECURITY.md), [`docs/release_checklist.md`](docs/release_checklist.md), [`CITATION.cff`](CITATION.cff)
- Windows + VS Code testing: [`docs/testing_on_windows.md`](docs/testing_on_windows.md)

## License

- **Code** (`src/`, `scripts/`, `dashboard/`, `tests/`, `configs/`, `Dockerfile`, `compose.yaml`, `Makefile`, `.github/`): [Apache License 2.0](LICENSE). See also [`NOTICE`](NOTICE).
- **Documentation, figures, presentation and experiment results** (`docs/`, `presentation/`, `results/`, this README's text): [Creative Commons Attribution 4.0 International](LICENSES/CC-BY-4.0.txt).

Copyright 2026 Parv Agarwal. Independent mini-project, not an officially endorsed IIT Patna project.
