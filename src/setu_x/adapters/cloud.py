"""Real-provider adapter skeletons: AWS, Azure, GCP.

Status in this release: configuration validation only. No provider SDK is installed and no
provider API is called, so these adapters can reach at most `configured`. Every operation is
`not_implemented` and raises. Mutating operations (deploy, cleanup) check the cloud-mode safety
rails first.

Configuration comes from environment variables. Credentials are NEVER read from SETU-X config:
when implemented, each adapter will use the provider's standard credential chain (AWS
profiles/SSO, `az login` / managed identity, gcloud ADC). See docs/cloud_setup.md.

Rules:
  - no SETU_<PROVIDER>_* variables set      -> not_configured
  - all required variables set and valid    -> configured
  - some set, or any value invalid          -> AdapterConfigError naming the variable(s)
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from setu_x.adapters.base import AdapterConfigError, AdapterStatus, CloudAdapter, Operation, OperationSupport


class AwsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    target: str = Field(default="ecs-express", pattern=r"^ecs-express$")
    execution_role_arn: str = Field(pattern=r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")
    infrastructure_role_arn: str = Field(pattern=r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")


class AzureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
    resource_group: str = Field(pattern=r"^[-\w._()]{1,90}$")
    location: str = Field(pattern=r"^[a-z0-9]+$")
    containerapps_environment: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


class GcpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+\d+$")


class _ProviderSkeleton(CloudAdapter):
    prefix: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    target_description: ClassVar[str]
    optional_keys: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        super().__init__(environ)
        self.config: BaseModel | None = self._load_config()
        reason = (f"{self.target_description}: not implemented in this release (no provider SDK is installed; "
                  "see docs/cloud_setup.md for the planned operations, permissions and smoke test)")
        self.reasons = {op: reason for op in Operation}
        self.reasons[Operation.ESTIMATE_COST] = ("not implemented: no live pricing integration; review the "
                                                 "provider's official pricing calculator")

    def _load_config(self) -> BaseModel | None:
        fields = self.config_model.model_fields
        present = {name: self._environ[self.prefix + name.upper()] for name in fields
                   if self._environ.get(self.prefix + name.upper())}
        if not present:
            return None
        try:
            return self.config_model.model_validate(present)
        except ValidationError as exc:
            problems = [f"{self.prefix}{str(e['loc'][0]).upper()}: {e['msg']}" for e in exc.errors()]
            raise AdapterConfigError(f"{self.name}: invalid configuration: " + "; ".join(problems)) from None

    def capabilities(self) -> dict[Operation, OperationSupport]:
        return {op: OperationSupport.NOT_IMPLEMENTED for op in Operation}

    def _claimed_status(self) -> AdapterStatus:
        return AdapterStatus.CONFIGURED if self.config is not None else AdapterStatus.NOT_CONFIGURED

    def status_detail(self) -> str:
        if self.config is None:
            required = ", ".join(self.prefix + f.upper() for f, info in self.config_model.model_fields.items()
                                 if info.is_required())
            return f"not configured (set {required}); target: {self.target_description}"
        return f"configuration validated; no provider API call made; target: {self.target_description}"


class AwsAdapter(_ProviderSkeleton):
    name, provider, prefix = "aws", "aws", "SETU_AWS_"
    config_model = AwsConfig
    target_description = "Amazon ECS Express Mode (Fargate)"


class AzureAdapter(_ProviderSkeleton):
    name, provider, prefix = "azure", "azure", "SETU_AZURE_"
    config_model = AzureConfig
    target_description = "Azure Container Apps"


class GcpAdapter(_ProviderSkeleton):
    name, provider, prefix = "gcp", "gcp", "SETU_GCP_"
    config_model = GcpConfig
    target_description = "Google Cloud Run"
