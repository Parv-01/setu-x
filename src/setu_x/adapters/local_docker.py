"""Local Docker adapter: the services running on this Docker host (including simulated-label envs).

Implemented over plain HTTP: environment discovery and endpoint discovery (from the validated
config), health checks and service status (GET /health/ready). Container lifecycle (deploy,
cleanup), logs and metrics are deliberately NOT implemented: the adapter runs inside a container
without access to the Docker API (no socket mount), and those actions belong to `docker compose`
on the host.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from setu_x.adapters.base import (
    AdapterStatus,
    CloudAdapter,
    DeploymentEnvironment,
    HealthStatus,
    Operation,
    OperationSupport,
    ServiceEndpoint,
    ServiceStatus,
)
from setu_x.placement import EnvironmentsConfig, PlacementConfig

IMPL, NOT_IMPL, UNSUP = OperationSupport.IMPLEMENTED, OperationSupport.NOT_IMPLEMENTED, OperationSupport.UNSUPPORTED


class LocalDockerAdapter(CloudAdapter):
    name = "local-docker"
    provider = "local"
    reasons = {
        Operation.DEPLOY_SERVICE: "container lifecycle is managed by `docker compose` on the host; the adapter has "
        "no Docker API access by design (no socket mount)",
        Operation.CLEANUP: "use `docker compose down` on the host",
        Operation.FETCH_LOGS: "use `docker compose logs` on the host",
        Operation.FETCH_METRICS: "use `docker stats` on the host (Phase 5 records it from the host side)",
        Operation.ESTIMATE_COST: "local execution has no provider billing; Experiment H uses an explicit, "
        "illustrative cost model instead",
    }

    def __init__(
        self,
        envs: EnvironmentsConfig,
        placement: PlacementConfig,
        client: httpx.AsyncClient,
        environ: Mapping[str, str] | None = None,
        extra_endpoints: list[ServiceEndpoint] | None = None,
    ) -> None:
        super().__init__(environ)
        self.envs, self.placement, self.client = envs, placement, client
        self.extra_endpoints = extra_endpoints or []

    def update(self, envs: EnvironmentsConfig, placement: PlacementConfig) -> None:
        self.envs, self.placement = envs, placement

    def capabilities(self) -> dict[Operation, OperationSupport]:
        return {
            Operation.DISCOVER_ENVIRONMENTS: IMPL,
            Operation.DEPLOY_SERVICE: NOT_IMPL,
            Operation.SERVICE_STATUS: IMPL,
            Operation.HEALTH_CHECK: IMPL,
            Operation.DISCOVER_ENDPOINTS: IMPL,
            Operation.FETCH_LOGS: NOT_IMPL,
            Operation.FETCH_METRICS: NOT_IMPL,
            Operation.CLEANUP: NOT_IMPL,
            Operation.ESTIMATE_COST: UNSUP,
        }

    def _claimed_status(self) -> AdapterStatus:
        return AdapterStatus.LOCAL

    def status_detail(self) -> str:
        sim = [n for n, e in self.envs.environments.items() if e.simulated]
        return f"local Docker host; simulated environment labels served here: {', '.join(sim) or 'none'}"

    async def discover_environments(self) -> list[DeploymentEnvironment]:
        return [DeploymentEnvironment(n, e.provider, e.simulated, e.description)
                for n, e in self.envs.environments.items()]

    async def discover_endpoints(self) -> list[ServiceEndpoint]:
        out = list(self.extra_endpoints)
        for svc_name, svc in self.placement.service_items():
            for i in svc.instances:
                out.append(ServiceEndpoint(svc_name, i.name, i.env, i.url, self.envs.is_simulated(i.env),
                                           i.role, i.enabled))
        return out

    async def health_check(self, endpoint: ServiceEndpoint) -> HealthStatus:
        start = time.perf_counter()
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        try:
            r = await self.client.get(endpoint.url.rstrip("/") + "/health/ready", timeout=1.0)
            status = "ready" if r.status_code == 200 else "not_ready"
            return HealthStatus(endpoint.instance, endpoint.env, endpoint.simulated, status, r.status_code,
                                round((time.perf_counter() - start) * 1000, 3), now)
        except httpx.HTTPError as exc:
            return HealthStatus(endpoint.instance, endpoint.env, endpoint.simulated, "unreachable", None,
                                round((time.perf_counter() - start) * 1000, 3), now, type(exc).__name__)

    async def service_status(self, name: str) -> ServiceStatus:
        for ep in await self.discover_endpoints():
            if ep.instance == name:
                h = await self.health_check(ep)
                return ServiceStatus(name, h.status, h.detail)
        return ServiceStatus(name, "unknown", "not in placement")
