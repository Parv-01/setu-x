# Real-Cloud Integration (Mode 3): Status and Plan

SETU-X · Parv Agarwal · Phase 3 · 2026-10-25

> **Current status: no real-cloud integration is implemented.** The AWS, Azure and GCP adapters validate configuration only. With valid configuration they report `configured`, and they never go further, because **no provider API is called**. No adapter is `connected`, `deployed` or `verified`. The local release needs no cloud account or credentials.

Citation keys refer to `docs/references.bib`.

## 1. Where real-cloud code lives

```
src/setu_x/adapters/base.py          CloudAdapter contract, Operation / OperationSupport / AdapterStatus, safety guards
src/setu_x/adapters/local_docker.py  local Docker host (status: local), health + endpoint discovery implemented
src/setu_x/adapters/cloud.py         AwsAdapter, AzureAdapter, GcpAdapter skeletons (config validation only)
tests/contract/test_adapter_contract.py  the same contract tests run against all four adapters
```

Application code (gateway, services, controller) never imports a provider SDK. Provider-specific code stays inside its adapter.

## 2. Status ladder and how each level is earned

| Status | Meaning | Earned by | Enforced in code |
|---|---|---|---|
| `local` | the local Docker host | always, for `local-docker` | — |
| `simulated` | a `*-simulated` label on the local host | config validation (`environments.yaml`) | names must end in `-simulated` and set `simulated: true` |
| `not_configured` | no `SETU_<PROVIDER>_*` variables set | default | — |
| `configured` | all required variables present and valid | `AdapterConfigError` otherwise (names the variable) | partial config is an error, not a silent downgrade |
| `connected` | an authenticated provider API call succeeded | a passing `connection` Evidence record | `status()` raises if claimed without evidence |
| `deployed` | a real resource was created or updated | a passing `deployment` Evidence record, plus recorded resource IDs | same |
| `verified` | deployment smoke test and application smoke test passed | a passing `verification` Evidence record | same; failed evidence does not count |

## 3. Chosen container targets (from official documentation, accessed 2026-10-25)

| | AWS | Azure | GCP |
|---|---|---|---|
| Target | **Amazon ECS Express Mode** (Fargate) [awsecsexpress; awsfargate] | **Azure Container Apps** [azurecontainerapps] | **Cloud Run** services [gcpcloudrun] |
| Why | Takes a container image plus an execution role and an infrastructure role, and creates a Fargate service, Application Load Balancer with TLS, auto scaling and a URL | Serverless containers with HTTPS/TCP ingress, internal ingress with DNS service discovery, revisions and scale rules | Fully managed containers with an HTTPS `*.run.app` endpoint, scale to zero, request- or instance-based billing |
| Rejected alternative | **AWS App Runner**: no longer open to new customers; AWS recommends ECS Express Mode [awsapprunnerchange] | Azure Kubernetes Service: more control than this project needs | GKE: same reason |
| Cost notes from the docs | Express Mode itself has no extra charge; Fargate, the ALB, CloudWatch and data transfer are billed [awsecsexpress] | No usage charges when scaled to zero; idle replicas may be billed at a lower rate [azurecontainerapps] | A free tier exists under both billing models [gcpcloudrun] |

**No free-tier or price claims are made here.** Offers, regional prices, account eligibility and quotas change. Check the provider's own pricing calculator and current free-tier terms before creating anything.

### Compose service → provider mapping

| Compose service | AWS | Azure | GCP |
|---|---|---|---|
| `gateway` (public) | Express Mode service with a public HTTPS URL | Container App with external ingress | Cloud Run service, public URL |
| `inventory-*`, `pricing` | Express Mode service (private, or HTTPS with service-to-service auth if called across providers) | Container App with internal ingress | Cloud Run service with restricted ingress or authenticated invocation |
| `controller` | stays local in the first real-cloud experiments (it is a lab control plane, not a production component) | same | same |

**Caveats**
- Identical images do not guarantee identical behaviour: cold starts, scale-to-zero, CPU allocation, TLS termination and egress paths differ by platform.
- Even locally, the same images failed differently on two Docker platforms. A stopped dependency produced an immediate connection refusal (about 0.03 s) on Linux Docker Engine 29.9.2, but a 0.3 s connect timeout on Docker Desktop 29.4.2 for Windows (see `docs/evidence/phase2_acceptance_windows.log`).

## 4. Configuration (environment variables; never credentials)

| Provider | Required variables | Validation |
|---|---|---|
| AWS | `SETU_AWS_REGION`, `SETU_AWS_EXECUTION_ROLE_ARN`, `SETU_AWS_INFRASTRUCTURE_ROLE_ARN` | region like `ap-south-1`; role ARNs like `arn:aws:iam::<12 digits>:role/<name>` |
| Azure | `SETU_AZURE_SUBSCRIPTION_ID`, `SETU_AZURE_RESOURCE_GROUP`, `SETU_AZURE_LOCATION`, `SETU_AZURE_CONTAINERAPPS_ENVIRONMENT` | GUID; resource-group characters; lowercase location; environment name |
| GCP | `SETU_GCP_PROJECT_ID`, `SETU_GCP_REGION` | project-ID format; region like `asia-south1` |

**Authentication (when implemented):** only the provider's standard credential chain is used:
- AWS: profiles or IAM Identity Center via the AWS SDK default chain.
- Azure: `az login`, workload identity or managed identity via the Azure Identity library.
- GCP: Application Default Credentials.

Keys and tokens are never read from SETU-X configuration, never stored in the repository, and never written to experiment results.

## 5. Safety rails (implemented and tested now)

- **Off by default.** Any create, change or delete operation raises `CloudOperationsDisabled` unless **both** `SETU_MODE=cloud` and `SETU_ALLOW_CLOUD_OPERATIONS=yes` are set.
- **Explicit confirmation.** Even when enabled, a mutating call raises `ConfirmationRequired` unless `confirm=True` is passed. The future CLI will first print provider, region, resources and cost-estimate status.
- **Order of checks.** These guards run **before** any other logic, so a mistaken call cannot reach a provider. The contract tests check this for every adapter.
- **No fabricated data.** Unimplemented operations raise `OperationNotSupported` with a reason. They never return placeholder data.
- **Status needs evidence.** `connected`, `deployed` and `verified` cannot be claimed without passing evidence records.

## 6. Planned operations per provider (not implemented)

| Operation | Plan | Required permissions |
|---|---|---|
| connect | one read-only identity call (e.g. AWS STS `GetCallerIdentity`, an Azure subscription read, a GCP project read) | read-only identity: **to be confirmed against official docs at implementation time** |
| deploy_service | create or update one service per Compose service from a pushed image; record every resource ID in `results/cloud_resources.json` | create/update on the target service and its networking: **not yet verified; must be least-privilege and documented before first use** |
| health_check / discover_endpoints | HTTPS GET `/health/ready` on the provider URL | none beyond network access |
| fetch_logs / fetch_metrics | provider logging/metrics APIs (CloudWatch, Log Analytics, Cloud Logging) | read-only logging/monitoring |
| cleanup | delete exactly the recorded resource IDs; report any that could not be deleted | delete on those resource types |
| estimate_cost | not planned to be automatic: users review the official pricing calculator | — |

**Image registry:** each provider needs the image in a registry it can pull from (ECR, ACR, Artifact Registry, or a public registry). Pushing images is a separate, explicit step.

## 7. Smoke test required before any adapter may report `verified`

1. `connect` succeeds → record `connection` evidence.
2. Deploy the stack (or one service) → record resource IDs and `deployment` evidence.
3. `GET /health/ready` on every deployed service returns 200.
4. One `POST /api/orders` through the deployed gateway returns `confirmed` with the golden values (`SKU-001`, qty 2 → total 11000).
5. Cleanup removes every recorded resource. Anything left is reported.
6. Only if steps 1–5 pass, record `verification` evidence.

A real-cloud performance comparison additionally needs, for each provider: region, resource configuration, network path, service tier, runtime settings, workload, number of repeated runs across different times of day, and cost assumptions. Cloud performance varies by provider, over time and with multitenancy [leitner2016patterns; iosup2011variability; uta2020bigdata].
