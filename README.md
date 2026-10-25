# SETU-X

**S**ervice portability · **E**xperimentation · **T**esting · **U**nified orchestration · **X** cross-environment execution and evaluation

*Setu* is Sanskrit for "bridge". The expansion is a project-specific backronym, not a Sanskrit phrase.

SETU-X is a local-first experimental platform. It shows how a small containerized microservice application behaves when its services are placed across heterogeneous environments: local, plus **simulated** AWS, Azure and GCP. It also shows how network delay, failures and resilience policies change latency and availability.

> **Simulation notice.** Labels such as `aws-simulated` are logical environments on one Docker host. They are **not** deployments on AWS, Azure or Google Cloud, and results from them are not measurements of those providers.

**Author:** Parv Agarwal, Junior Research Associate (Technical), AI NLP ML Research Laboratory, IIT Patna. Independent mini-project, not an officially endorsed IIT Patna project.

**Status:** Phase 2 of 11, minimal runnable microservices (gateway, inventory, pricing). See [`docs/handover.md`](docs/handover.md) for current progress.

## Quick start (Docker only)

Requirements: Docker Engine with Docker Compose v5.1.3 or later. Docker Desktop on Windows/macOS must show "Engine running".

```bash
docker compose config              # validate the configuration
docker compose up --build -d       # build and start (gateway on http://127.0.0.1:8080)
docker compose ps                  # all services should be "healthy"
curl -s -X POST http://127.0.0.1:8080/api/orders \
  -H 'content-type: application/json' \
  -d '{"sku":"SKU-001","quantity":2,"customer_tier":"standard"}'
docker compose logs gateway        # structured JSON logs
docker compose down                # stop
```

Interactive API docs: http://127.0.0.1:8080/docs

On Windows PowerShell, use `Invoke-RestMethod` (see [`docs/testing_on_windows.md`](docs/testing_on_windows.md)).

## Tests

```bash
docker compose --profile test run --rm tests                 # unit + integration (no running stack needed)
docker compose --profile test run --rm tests pytest -m e2e   # end-to-end (stack must be up)
bash scripts/phase2_acceptance.sh                            # full Phase 2 acceptance run (Linux/macOS/Git Bash)
pwsh -ExecutionPolicy Bypass -File scripts/phase2_acceptance.ps1   # same checks in PowerShell 7 (Windows)
```

## Documentation

- Research question and hypotheses: [`docs/research_questions.md`](docs/research_questions.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Literature review: [`docs/literature_review.md`](docs/literature_review.md), bibliography [`docs/references.bib`](docs/references.bib)
- Implementation plan: [`docs/implementation_plan.md`](docs/implementation_plan.md)
- Windows + VS Code testing: [`docs/testing_on_windows.md`](docs/testing_on_windows.md)

## License

Not yet chosen (Phase 9). Until then, all rights reserved by the author.
