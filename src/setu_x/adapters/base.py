"""Provider-neutral adapter contract.

Every adapter declares, per operation, whether it is
    implemented              the method does real work and returns real data
    not_implemented          the provider could support it, SETU-X does not (yet); the method raises
    unsupported_by_provider  the concept does not apply to this provider; the method raises
An adapter never returns fabricated data for an operation that is not implemented.

Integration status (what has actually been demonstrated, never used interchangeably):
    local           the local Docker host itself; no cloud provider involved
    simulated       logical environment label on the local Docker host
    not_configured  real-provider adapter without (complete) configuration
    configured      required configuration supplied and validated; NO provider call made
    connected       an authenticated provider API call succeeded (requires ConnectionEvidence)
    deployed        a real resource was created/updated (requires DeploymentEvidence)
    verified        deployment + application smoke tests passed (requires VerificationEvidence)
`connected` does not imply `deployed`; `deployed` does not imply a healthy application.

Safety rails for operations that create, change or delete cloud resources (deploy, cleanup):
    1. cloud operations must be enabled: SETU_MODE=cloud and SETU_ALLOW_CLOUD_OPERATIONS=yes
    2. the caller must pass confirm=True (after showing provider, region, resources, cost status)
These checks run BEFORE anything else, so a mistaken call can never reach a provider.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Operation(StrEnum):
    DISCOVER_ENVIRONMENTS = "discover_environments"
    DEPLOY_SERVICE = "deploy_service"
    SERVICE_STATUS = "service_status"
    HEALTH_CHECK = "health_check"
    DISCOVER_ENDPOINTS = "discover_endpoints"
    FETCH_LOGS = "fetch_logs"
    FETCH_METRICS = "fetch_metrics"
    CLEANUP = "cleanup"
    ESTIMATE_COST = "estimate_cost"


MUTATING_OPERATIONS = frozenset({Operation.DEPLOY_SERVICE, Operation.CLEANUP})


class OperationSupport(StrEnum):
    IMPLEMENTED = "implemented"
    NOT_IMPLEMENTED = "not_implemented"
    UNSUPPORTED = "unsupported_by_provider"


class AdapterStatus(StrEnum):
    LOCAL = "local"
    SIMULATED = "simulated"
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    CONNECTED = "connected"
    DEPLOYED = "deployed"
    VERIFIED = "verified"


# ------------------------------------------------------------------------------------- data types
@dataclass(frozen=True)
class DeploymentEnvironment:
    name: str
    provider: str
    simulated: bool
    description: str = ""


@dataclass(frozen=True)
class ServiceEndpoint:
    service: str
    instance: str
    env: str
    url: str
    simulated: bool
    role: str = "secondary"
    enabled: bool = True


@dataclass(frozen=True)
class HealthStatus:
    instance: str
    env: str
    simulated: bool
    status: str  # "ready" | "not_ready" | "unreachable" | "error"
    http_status: int | None
    latency_ms: float | None
    checked_at: str
    detail: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    image: str
    port: int = 8000
    env: Mapping[str, str] = field(default_factory=dict)
    cpu: float = 0.5
    memory_mib: int = 256


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: str
    detail: str = ""


@dataclass(frozen=True)
class ResourceObservation:
    name: str
    metric: str
    value: float
    unit: str
    observed_at: str


@dataclass(frozen=True)
class DeploymentResult:
    resource_ids: tuple[str, ...]
    endpoint: str | None
    detail: str = ""


@dataclass(frozen=True)
class CleanupResult:
    removed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class LogResult:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class CostEstimate:
    available: bool
    detail: str
    amount: float | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Proof that a status level was reached (who/what/when). Required for connected and above."""

    kind: str  # "connection" | "deployment" | "verification"
    passed: bool
    checked_at: datetime
    description: str


# ----------------------------------------------------------------------------------------- errors
class AdapterError(Exception):
    pass


class OperationNotSupported(AdapterError):
    def __init__(self, adapter: str, operation: Operation, support: OperationSupport, reason: str) -> None:
        self.adapter, self.operation, self.support, self.reason = adapter, operation, support, reason
        super().__init__(f"{adapter}.{operation.value} is {support.value}: {reason}")


class CloudOperationsDisabled(AdapterError):
    pass


class ConfirmationRequired(AdapterError):
    pass


class AdapterConfigError(AdapterError, ValueError):
    pass


def cloud_operations_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get("SETU_MODE") == "cloud" and env.get("SETU_ALLOW_CLOUD_OPERATIONS") == "yes"


# ------------------------------------------------------------------------------------------ base
class CloudAdapter(ABC):
    name: str
    provider: str
    reasons: dict[Operation, str] = {}

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        self.evidence: list[Evidence] = []

    # --- declared support and status ------------------------------------------------------------
    @abstractmethod
    def capabilities(self) -> dict[Operation, OperationSupport]: ...

    @abstractmethod
    def _claimed_status(self) -> AdapterStatus: ...

    def status(self) -> AdapterStatus:
        """Return the claimed status after checking it is backed by evidence."""
        claimed = self._claimed_status()
        required = {
            AdapterStatus.CONNECTED: "connection",
            AdapterStatus.DEPLOYED: "deployment",
            AdapterStatus.VERIFIED: "verification",
        }.get(claimed)
        if required and not any(e.kind == required and e.passed for e in self.evidence):
            raise AdapterError(f"{self.name}: status '{claimed.value}' claimed without passing {required} evidence")
        return claimed

    def status_detail(self) -> str:
        return ""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "status": self.status().value,
            "status_detail": self.status_detail(),
            "capabilities": {op.value: sup.value for op, sup in self.capabilities().items()},
            "reasons": {op.value: r for op, r in self.reasons.items()},
            "evidence": [asdict(e) | {"checked_at": e.checked_at.isoformat()} for e in self.evidence],
        }

    # --- guards ---------------------------------------------------------------------------------
    def _unsupported(self, op: Operation) -> OperationNotSupported:
        support = self.capabilities()[op]
        return OperationNotSupported(self.name, op, support, self.reasons.get(op, "no reason recorded"))

    def _guard_mutation(self, op: Operation, confirm: bool) -> None:
        if not cloud_operations_enabled(self._environ):
            raise CloudOperationsDisabled(
                f"{self.name}.{op.value}: cloud operations are disabled "
                "(require SETU_MODE=cloud and SETU_ALLOW_CLOUD_OPERATIONS=yes)"
            )
        if not confirm:
            raise ConfirmationRequired(
                f"{self.name}.{op.value}: explicit confirm=True is required after reviewing provider, region, "
                "resources and the cost-estimate status"
            )

    # --- operations (default: not supported; subclasses override what they implement) ------------
    async def discover_environments(self) -> list[DeploymentEnvironment]:
        raise self._unsupported(Operation.DISCOVER_ENVIRONMENTS)

    async def deploy_service(self, spec: ServiceSpec, *, confirm: bool = False) -> DeploymentResult:
        self._guard_mutation(Operation.DEPLOY_SERVICE, confirm)
        raise self._unsupported(Operation.DEPLOY_SERVICE)

    async def service_status(self, name: str) -> ServiceStatus:
        raise self._unsupported(Operation.SERVICE_STATUS)

    async def health_check(self, endpoint: ServiceEndpoint) -> HealthStatus:
        raise self._unsupported(Operation.HEALTH_CHECK)

    async def discover_endpoints(self) -> list[ServiceEndpoint]:
        raise self._unsupported(Operation.DISCOVER_ENDPOINTS)

    async def fetch_logs(self, name: str, since: datetime | None = None) -> LogResult:
        raise self._unsupported(Operation.FETCH_LOGS)

    async def fetch_metrics(self, name: str) -> list[ResourceObservation]:
        raise self._unsupported(Operation.FETCH_METRICS)

    async def cleanup(self, resource_ids: list[str], *, confirm: bool = False) -> CleanupResult:
        self._guard_mutation(Operation.CLEANUP, confirm)
        raise self._unsupported(Operation.CLEANUP)

    async def estimate_cost(self, spec: ServiceSpec) -> CostEstimate:
        raise self._unsupported(Operation.ESTIMATE_COST)
