"""Every adapter honours the same contract (no credentials needed).

1. capabilities() covers every Operation with a valid OperationSupport.
2. implemented operations return the declared types; others raise OperationNotSupported carrying
   the declared support level and a reason - never fabricated data.
3. status() is an AdapterStatus; connected/deployed/verified are impossible without evidence.
4. mutating operations are blocked unless cloud mode + explicit allow + confirm=True, and the
   guard runs before anything else.
5. real-provider adapters: no config -> not_configured; valid config -> configured (never more);
   partial or invalid config -> AdapterConfigError naming the variable.
"""
from datetime import UTC, datetime

import httpx
import pytest

from setu_x.adapters import AwsAdapter, AzureAdapter, GcpAdapter, LocalDockerAdapter, build_adapters
from setu_x.adapters.base import (
    AdapterConfigError,
    AdapterError,
    AdapterStatus,
    CloudOperationsDisabled,
    ConfirmationRequired,
    DeploymentEnvironment,
    Evidence,
    HealthStatus,
    Operation,
    OperationNotSupported,
    OperationSupport,
    ServiceEndpoint,
    ServiceSpec,
    ServiceStatus,
)
from tests.conftest import build_config

VALID_ENV = {
    "SETU_AWS_REGION": "ap-south-1",
    "SETU_AWS_EXECUTION_ROLE_ARN": "arn:aws:iam::123456789012:role/setu-exec",
    "SETU_AWS_INFRASTRUCTURE_ROLE_ARN": "arn:aws:iam::123456789012:role/setu-infra",
    "SETU_AZURE_SUBSCRIPTION_ID": "00000000-1111-2222-3333-444444444444",
    "SETU_AZURE_RESOURCE_GROUP": "setu-x-rg",
    "SETU_AZURE_LOCATION": "centralindia",
    "SETU_AZURE_CONTAINERAPPS_ENVIRONMENT": "setu-x-env",
    "SETU_GCP_PROJECT_ID": "setu-x-demo",
    "SETU_GCP_REGION": "asia-south1",
}
SPEC = ServiceSpec(name="inventory", image="setu-x:0.3.0")


def ok_transport(request):
    return httpx.Response(200, json={"status": "ready"})


def make_adapters(environ=None):
    envs, placement = build_config()
    client = httpx.AsyncClient(transport=httpx.MockTransport(ok_transport))
    return [
        LocalDockerAdapter(envs, placement, client, environ or {}),
        AwsAdapter(environ or {}),
        AzureAdapter(environ or {}),
        GcpAdapter(environ or {}),
    ]


ALL = make_adapters()
IDS = [a.name for a in ALL]


@pytest.mark.parametrize("adapter", ALL, ids=IDS)
def test_capabilities_cover_every_operation(adapter):
    caps = adapter.capabilities()
    assert set(caps) == set(Operation)
    assert all(isinstance(v, OperationSupport) for v in caps.values())
    for op, sup in caps.items():
        if sup is not OperationSupport.IMPLEMENTED:
            assert adapter.reasons.get(op), f"{adapter.name}.{op.value} needs a reason"


async def call(adapter, op):
    ep = ServiceEndpoint("inventory", "inventory-a", "aws-simulated", "http://inventory-a:8000", True, "primary")
    return await {
        Operation.DISCOVER_ENVIRONMENTS: lambda: adapter.discover_environments(),
        Operation.DEPLOY_SERVICE: lambda: adapter.deploy_service(SPEC, confirm=True),
        Operation.SERVICE_STATUS: lambda: adapter.service_status("inventory-a"),
        Operation.HEALTH_CHECK: lambda: adapter.health_check(ep),
        Operation.DISCOVER_ENDPOINTS: lambda: adapter.discover_endpoints(),
        Operation.FETCH_LOGS: lambda: adapter.fetch_logs("inventory-a"),
        Operation.FETCH_METRICS: lambda: adapter.fetch_metrics("inventory-a"),
        Operation.CLEANUP: lambda: adapter.cleanup(["r-1"], confirm=True),
        Operation.ESTIMATE_COST: lambda: adapter.estimate_cost(SPEC),
    }[op]()


EXPECTED_TYPES = {
    Operation.DISCOVER_ENVIRONMENTS: (list, DeploymentEnvironment),
    Operation.DISCOVER_ENDPOINTS: (list, ServiceEndpoint),
    Operation.HEALTH_CHECK: (HealthStatus, None),
    Operation.SERVICE_STATUS: (ServiceStatus, None),
}


@pytest.mark.parametrize("op", list(Operation), ids=[o.value for o in Operation])
@pytest.mark.parametrize("adapter_index", range(4), ids=IDS)
async def test_operations_behave_as_declared(adapter_index, op):
    # cloud ops enabled so mutating calls get past the safety guard and reach the support check
    adapter = make_adapters({"SETU_MODE": "cloud", "SETU_ALLOW_CLOUD_OPERATIONS": "yes"})[adapter_index]
    support = adapter.capabilities()[op]
    if support is OperationSupport.IMPLEMENTED:
        result = await call(adapter, op)
        outer, inner = EXPECTED_TYPES[op]
        assert isinstance(result, outer)
        if inner:
            assert result and all(isinstance(x, inner) for x in result)
    else:
        with pytest.raises(OperationNotSupported) as exc:
            await call(adapter, op)
        assert exc.value.support is support and exc.value.reason


@pytest.mark.parametrize("adapter", ALL, ids=IDS)
async def test_mutations_blocked_without_cloud_mode_and_confirmation(adapter):
    for coro in (lambda: adapter.deploy_service(SPEC, confirm=True), lambda: adapter.cleanup(["x"], confirm=True)):
        with pytest.raises(CloudOperationsDisabled):
            await coro()
    def rebuild(environ):
        args = [adapter.envs, adapter.placement, adapter.client] if isinstance(adapter, LocalDockerAdapter) else []
        return type(adapter)(*args, environ=environ)

    enabled = rebuild({"SETU_MODE": "cloud", "SETU_ALLOW_CLOUD_OPERATIONS": "yes"})
    with pytest.raises(ConfirmationRequired):
        await enabled.deploy_service(SPEC)
    with pytest.raises(ConfirmationRequired):
        await enabled.cleanup(["x"])
    # allow flag alone (without cloud mode) is not enough
    half = rebuild({"SETU_ALLOW_CLOUD_OPERATIONS": "yes"})
    with pytest.raises(CloudOperationsDisabled):
        await half.deploy_service(SPEC, confirm=True)


def test_status_values_without_config():
    statuses = {a.name: a.status() for a in make_adapters()}
    assert statuses == {"local-docker": AdapterStatus.LOCAL, "aws": AdapterStatus.NOT_CONFIGURED,
                        "azure": AdapterStatus.NOT_CONFIGURED, "gcp": AdapterStatus.NOT_CONFIGURED}


def test_valid_config_reaches_configured_and_never_more():
    for a in make_adapters(VALID_ENV)[1:]:
        assert a.status() is AdapterStatus.CONFIGURED
        assert "no provider API call made" in a.status_detail()
        assert a.describe()["evidence"] == []


@pytest.mark.parametrize("cls,env,var", [
    (AwsAdapter, {"SETU_AWS_REGION": "ap-south-1"}, "SETU_AWS_EXECUTION_ROLE_ARN"),            # partial
    (AwsAdapter, {**VALID_ENV, "SETU_AWS_REGION": "mumbai"}, "SETU_AWS_REGION"),               # invalid
    (AzureAdapter, {**VALID_ENV, "SETU_AZURE_SUBSCRIPTION_ID": "abc"}, "SETU_AZURE_SUBSCRIPTION_ID"),
    (GcpAdapter, {"SETU_GCP_PROJECT_ID": "setu-x-demo"}, "SETU_GCP_REGION"),
])
def test_invalid_or_partial_config_names_variable(cls, env, var):
    with pytest.raises(AdapterConfigError) as exc:
        cls(env)
    assert var in str(exc.value)


def test_claimed_status_without_evidence_is_rejected():
    class Liar(AwsAdapter):
        def _claimed_status(self):
            return AdapterStatus.VERIFIED

    liar = Liar(VALID_ENV)
    with pytest.raises(AdapterError, match="without passing verification evidence"):
        liar.status()
    liar.evidence.append(Evidence("verification", False, datetime.now(UTC), "smoke test failed"))
    with pytest.raises(AdapterError):
        liar.status()  # failed evidence does not count
    liar.evidence.append(Evidence("verification", True, datetime.now(UTC), "smoke test passed"))
    assert liar.status() is AdapterStatus.VERIFIED


def test_registry_reports_config_errors_without_breaking_local():
    envs, placement = build_config()
    client = httpx.AsyncClient(transport=httpx.MockTransport(ok_transport))
    adapters, errors = build_adapters(envs, placement, client, {"SETU_GCP_PROJECT_ID": "setu-x-demo"})
    assert set(adapters) == {"local-docker", "aws", "azure"} and "SETU_GCP_REGION" in errors["gcp"]


async def test_local_adapter_marks_simulated_environments():
    local = make_adapters()[0]
    envs = {e.name: e.simulated for e in await local.discover_environments()}
    assert envs == {"local": False, "aws-simulated": True, "azure-simulated": True, "gcp-simulated": True}
    eps = await local.discover_endpoints()
    assert {(e.instance, e.env, e.simulated) for e in eps} == {
        ("inventory-a", "aws-simulated", True), ("inventory-b", "gcp-simulated", True),
        ("pricing", "azure-simulated", True)}


async def test_local_adapter_health_check_reports_unreachable():
    envs, placement = build_config()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    local = LocalDockerAdapter(envs, placement, httpx.AsyncClient(transport=httpx.MockTransport(refuse)), {})
    ep = (await local.discover_endpoints())[0]
    h = await local.health_check(ep)
    assert h.status == "unreachable" and h.http_status is None and h.detail == "ConnectError"
