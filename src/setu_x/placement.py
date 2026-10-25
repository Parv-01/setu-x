"""Environment, placement, routing and resilience configuration models with validation.

Two YAML files under configs/:
  environments.yaml  environments (local + *-simulated labels) and the inter-environment RTT matrix
  placement.yaml     which service instance runs in which environment, routing policy, resilience

Every rule here has a unit test (tests/unit/test_placement_config.py). Validation errors name the
file and the field path, e.g.
  "configs/placement.yaml: services.inventory.instances.0.env: unknown environment 'aws-sim'".
"""
from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from setu_x.common.config import ConfigError

NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
URL_PATTERN = r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$"
Provider = Literal["local", "aws", "azure", "gcp"]
MAX_LINK_RTT_MS = 2000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------- environments
class Environment(_Strict):
    provider: Provider
    simulated: bool
    description: str = ""


class EnvironmentsConfig(_Strict):
    environments: dict[str, Environment]
    link_rtt_ms: dict[str, int] = Field(default_factory=dict)

    @field_validator("environments")
    @classmethod
    def _names_and_labels(cls, envs: dict[str, Environment]) -> dict[str, Environment]:
        import re

        if not envs:
            raise ValueError("at least one environment is required")
        for name, env in envs.items():
            if not re.match(NAME_PATTERN, name):
                raise ValueError(f"environment name '{name}' must match {NAME_PATTERN}")
            if env.simulated:
                if env.provider == "local":
                    raise ValueError(f"'{name}': provider 'local' cannot be simulated")
                if not name.endswith("-simulated"):
                    raise ValueError(f"'{name}': simulated environments must be named '<something>-simulated'")
            else:
                if env.provider != "local":
                    raise ValueError(
                        f"'{name}': non-simulated {env.provider} environments are not allowed in this config; "
                        "real-cloud targets are handled by provider adapters in cloud mode"
                    )
                if name.endswith("-simulated"):
                    raise ValueError(f"'{name}': name says simulated but simulated is false")
        if not any(e.provider == "local" and not e.simulated for e in envs.values()):
            raise ValueError("exactly one non-simulated 'local' environment is required")
        if sum(1 for e in envs.values() if e.provider == "local") > 1:
            raise ValueError("only one 'local' environment is allowed")
        return envs

    @model_validator(mode="after")
    def _links(self) -> EnvironmentsConfig:
        seen: dict[frozenset, str] = {}
        for key, value in self.link_rtt_ms.items():
            parts = key.split("--")
            if len(parts) != 2:
                raise ValueError(f"link_rtt_ms key '{key}' must look like '<env-a>--<env-b>'")
            a, b = parts
            for e in (a, b):
                if e not in self.environments:
                    raise ValueError(f"link_rtt_ms key '{key}': unknown environment '{e}'")
            if a == b:
                raise ValueError(f"link_rtt_ms key '{key}': same-environment links are implicitly 0 ms")
            pair = frozenset((a, b))
            if pair in seen:
                raise ValueError(f"link_rtt_ms: '{key}' duplicates '{seen[pair]}'")
            seen[pair] = key
            if not (0 <= value <= MAX_LINK_RTT_MS):
                raise ValueError(f"link_rtt_ms '{key}': {value} ms is outside 0..{MAX_LINK_RTT_MS}")
        missing = [f"{a}--{b}" for a, b in combinations(sorted(self.environments), 2)
                   if frozenset((a, b)) not in seen]
        if missing:
            raise ValueError("link_rtt_ms is missing pairs: " + ", ".join(missing))
        return self

    def rtt_ms(self, a: str, b: str) -> int:
        """Configured round-trip delay between two environments (0 within one environment)."""
        if a == b:
            return 0
        for key, value in self.link_rtt_ms.items():
            x, y = key.split("--")
            if {x, y} == {a, b}:
                return value
        raise KeyError(f"no link between '{a}' and '{b}'")

    def is_simulated(self, env: str) -> bool:
        return self.environments[env].simulated


# ---------------------------------------------------------------------------------------- placement
class Instance(_Strict):
    name: str = Field(pattern=NAME_PATTERN)
    env: str = Field(pattern=NAME_PATTERN)
    url: str = Field(pattern=URL_PATTERN)
    role: Literal["primary", "secondary"] = "secondary"
    weight: int = Field(default=1, ge=0, le=100)
    enabled: bool = True


class ServicePlacement(_Strict):
    routing: Literal["primary-fallback", "round-robin", "weighted"] = "primary-fallback"
    instances: list[Instance] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _instances(self) -> ServicePlacement:
        names = [i.name for i in self.instances]
        if len(names) != len(set(names)):
            raise ValueError("instance names must be unique within a service")
        primaries = [i.name for i in self.instances if i.role == "primary"]
        if len(primaries) != 1:
            raise ValueError(f"exactly one instance must have role 'primary' (found {len(primaries)})")
        if not any(i.enabled for i in self.instances):
            raise ValueError("at least one instance must be enabled")
        if self.routing == "weighted" and sum(i.weight for i in self.instances if i.enabled) == 0:
            raise ValueError("weighted routing needs a positive total weight across enabled instances")
        return self


class Backoff(_Strict):
    base_ms: int = Field(default=50, ge=1, le=1000)
    max_ms: int = Field(default=400, ge=1, le=5000)

    @model_validator(mode="after")
    def _order(self) -> Backoff:
        if self.max_ms < self.base_ms:
            raise ValueError("backoff.max_ms must be >= backoff.base_ms")
        return self


class CircuitBreaker(_Strict):
    enabled: bool = False
    consecutive_failures: int = Field(default=5, ge=1, le=100)
    reset_timeout_ms: int = Field(default=5000, ge=100, le=60_000)


class Resilience(_Strict):
    outer_deadline_ms: int = Field(default=3000, ge=100, le=10_000)
    timeout_ms: int = Field(default=800, ge=10, le=10_000)
    connect_timeout_ms: int = Field(default=300, ge=10, le=10_000)
    retries: int = Field(default=0, ge=0, le=3)
    backoff: Backoff = Field(default_factory=Backoff)
    circuit_breaker: CircuitBreaker = Field(default_factory=CircuitBreaker)
    fallback: bool = True

    @model_validator(mode="after")
    def _bounds(self) -> Resilience:
        if self.timeout_ms > self.outer_deadline_ms:
            raise ValueError("timeout_ms must not exceed outer_deadline_ms")
        if self.connect_timeout_ms > self.timeout_ms:
            raise ValueError("connect_timeout_ms must not exceed timeout_ms")
        return self


class Services(_Strict):
    inventory: ServicePlacement
    pricing: ServicePlacement


class PlacementConfig(_Strict):
    call_mode: Literal["parallel", "sequential"] = "parallel"
    services: Services
    resilience: Resilience = Field(default_factory=Resilience)

    @model_validator(mode="after")
    def _global_unique(self) -> PlacementConfig:
        seen: dict[str, str] = {}
        for svc_name, svc in self.service_items():
            for inst in svc.instances:
                if inst.name in seen:
                    raise ValueError(f"instance name '{inst.name}' is used by both {seen[inst.name]} and {svc_name}")
                seen[inst.name] = svc_name
        return self

    def service_items(self) -> list[tuple[str, ServicePlacement]]:
        return [("inventory", self.services.inventory), ("pricing", self.services.pricing)]

    def service(self, name: str) -> ServicePlacement:
        return getattr(self.services, name)

    def check_against(self, envs: EnvironmentsConfig) -> None:
        """Cross-file rule: every instance env must exist in environments.yaml."""
        for svc_name, svc in self.service_items():
            for idx, inst in enumerate(svc.instances):
                if inst.env not in envs.environments:
                    raise ConfigError(
                        f"services.{svc_name}.instances.{idx}.env: unknown environment '{inst.env}' "
                        f"(known: {', '.join(sorted(envs.environments))})"
                    )

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    def identity(self) -> set[tuple[str, str, str]]:
        """The fixed part of a placement: (service, instance name, url)."""
        return {(s, i.name, i.url) for s, svc in self.service_items() for i in svc.instances}


# ------------------------------------------------------------------------------------------ loading
def _format_errors(source: str, exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "").removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}")
    return f"{source}: " + "; ".join(parts)


def parse_environments(data: Any, source: str = "environments") -> EnvironmentsConfig:
    try:
        return EnvironmentsConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(source, exc)) from None


def parse_placement(data: Any, envs: EnvironmentsConfig, source: str = "placement") -> PlacementConfig:
    try:
        placement = PlacementConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(source, exc)) from None
    try:
        placement.check_against(envs)
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from None
    return placement


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file ({exc.strerror})") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc})") from None


def load_config_dir(config_dir: str | Path) -> tuple[EnvironmentsConfig, PlacementConfig]:
    d = Path(config_dir)
    envs = parse_environments(_read_yaml(d / "environments.yaml"), str(d / "environments.yaml"))
    placement = parse_placement(_read_yaml(d / "placement.yaml"), envs, str(d / "placement.yaml"))
    return envs, placement
