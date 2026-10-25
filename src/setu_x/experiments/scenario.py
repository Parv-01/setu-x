"""Experiment scenarios (configs/scenarios/*.yaml), validated before anything runs.

A scenario is a set of named CONDITIONS (levels of the independent variable), each repeated
`runs` times. Every (condition, run) pair is one RUN, the unit of replication: before each run the
platform is reset to configs/placement.yaml + environments.yaml, the condition is applied, all
random sources are reseeded, then load is generated for `warmup_s` (discarded) + `duration_s`
(measured).

Condition fields (all optional; anything not set stays at the file defaults):
    call_mode        parallel | sequential
    resilience       partial resilience settings, deep-merged (e.g. {retries: 2})
    routing          {service: policy}
    instance_env     {instance: env}      relabel placement (Experiment F)
    instance_enabled {instance: bool}
    links            {"<env-a>--<env-b>": ms}   partial link-matrix update (simulated mode only)
    rate_rps         offered load for this condition (Experiment E); default workload.rate_rps
    faults           scheduled faults, `at_s` seconds after the MEASUREMENT phase starts

`defaults` (same fields, no faults) is merged into every condition first; a condition's own values
win (dicts are deep-merged). The merged conditions are what the manifest records.

Every condition's resulting placement and link matrix is validated with the same rules as the
config files when the scenario is loaded, so an invalid scenario fails before any load is sent.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from setu_x.common.config import ConfigError
from setu_x.faults import FaultSpec
from setu_x.placement import EnvironmentsConfig, PlacementConfig, parse_environments, parse_placement

MAX_RATE_RPS = 100.0
MAX_RUN_SECONDS = 300
MAX_TOTAL_SECONDS = 2 * 3600
SKU_DEFAULT = ["SKU-002"]  # 36 units in stock: quantity 1 is always in stock (deterministic outcome)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Workload(_Strict):
    rate_rps: float = Field(gt=0, le=MAX_RATE_RPS)
    arrival: Literal["constant", "poisson"] = "constant"
    warmup_s: float = Field(default=5.0, ge=0, le=60)
    duration_s: float = Field(gt=0, le=MAX_RUN_SECONDS)
    max_in_flight: int = Field(default=64, ge=1, le=256)
    request_timeout_s: float = Field(default=10.0, gt=0, le=30)
    skus: list[str] = Field(default_factory=lambda: list(SKU_DEFAULT), min_length=1, max_length=50)
    quantity: int = Field(default=1, ge=1, le=100)
    customer_tier: Literal["standard", "gold"] = "standard"

    @model_validator(mode="after")
    def _skus(self) -> Workload:
        import re

        for s in self.skus:
            if not re.match(r"^SKU-[0-9]{3}$", s):
                raise ValueError(f"invalid sku '{s}'")
        return self


class ScheduledFault(FaultSpec):
    target: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    at_s: float = Field(ge=0, le=MAX_RUN_SECONDS)


class ConditionDefaults(_Strict):
    call_mode: Literal["parallel", "sequential"] | None = None
    resilience: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Literal["primary-fallback", "round-robin", "weighted"]] = Field(default_factory=dict)
    instance_env: dict[str, str] = Field(default_factory=dict)
    instance_enabled: dict[str, bool] = Field(default_factory=dict)
    instance_weight: dict[str, int] = Field(default_factory=dict)
    links: dict[str, int] = Field(default_factory=dict)
    rate_rps: float | None = Field(default=None, gt=0, le=MAX_RATE_RPS)


class Condition(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,48}$")
    description: str = ""
    rate_rps: float | None = Field(default=None, gt=0, le=MAX_RATE_RPS)
    call_mode: Literal["parallel", "sequential"] | None = None
    resilience: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Literal["primary-fallback", "round-robin", "weighted"]] = Field(default_factory=dict)
    instance_env: dict[str, str] = Field(default_factory=dict)
    instance_enabled: dict[str, bool] = Field(default_factory=dict)
    instance_weight: dict[str, int] = Field(default_factory=dict)
    links: dict[str, int] = Field(default_factory=dict)
    faults: list[ScheduledFault] = Field(default_factory=list, max_length=10)


class Scenario(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$")
    title: str
    hypotheses: list[str] = Field(default_factory=list)
    description: str = ""
    requires_mode: Literal["local", "simulated"] = "local"
    workload: Workload
    runs: int = Field(default=3, ge=1, le=20)
    order: Literal["randomized", "interleaved", "blocked"] = "randomized"
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    cooldown_s: float = Field(default=1.0, ge=0, le=30)
    recovery_window_s: float = Field(default=1.0, gt=0, le=10, description="rolling window for recovery")
    recovery_hold_s: float = Field(default=3.0, ge=0, le=60, description="success must hold this long")
    recovery_threshold: float = Field(default=0.95, gt=0, le=1, description="fraction of pre-fault success rate")
    defaults: ConditionDefaults = Field(default_factory=ConditionDefaults)
    conditions: list[Condition] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _checks(self) -> Scenario:
        d = self.defaults.model_dump(exclude_defaults=True)
        self.conditions = [Condition.model_validate(_deep_merge(d, c.model_dump(exclude_defaults=True)))
                           for c in self.conditions]
        names = [c.name for c in self.conditions]
        if len(names) != len(set(names)):
            raise ValueError("condition names must be unique")
        total = len(self.conditions) * self.runs * (self.workload.warmup_s + self.workload.duration_s + self.cooldown_s)
        if total > MAX_TOTAL_SECONDS:
            raise ValueError(f"scenario would run {total:.0f} s; the limit is {MAX_TOTAL_SECONDS} s")
        for c in self.conditions:
            for f in c.faults:
                if f.at_s >= self.workload.duration_s:
                    raise ValueError(f"condition '{c.name}': fault at_s={f.at_s} is not inside the "
                                     f"{self.workload.duration_s} s measurement window")
            if c.links and self.requires_mode != "simulated":
                raise ValueError(f"condition '{c.name}': link delays only take effect with requires_mode: simulated")
        return self

    def total_runs(self) -> int:
        return len(self.conditions) * self.runs

    def rate_for(self, condition: Condition) -> float:
        return condition.rate_rps or self.workload.rate_rps

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------- applying conditions
def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def placement_for(condition: Condition, base: PlacementConfig, envs: EnvironmentsConfig) -> PlacementConfig:
    """The placement a condition produces, validated with the same rules as placement.yaml."""
    raw = base.model_dump()
    if condition.call_mode:
        raw["call_mode"] = condition.call_mode
    raw["resilience"] = _deep_merge(raw["resilience"], condition.resilience)
    known = {i["name"] for svc in raw["services"].values() for i in svc["instances"]}
    for mapping, field in ((condition.instance_env, "env"), (condition.instance_enabled, "enabled"),
                           (condition.instance_weight, "weight")):
        for name, value in mapping.items():
            if name not in known:
                raise ConfigError(f"condition '{condition.name}': unknown instance '{name}'")
            for svc in raw["services"].values():
                for inst in svc["instances"]:
                    if inst["name"] == name:
                        inst[field] = value
    for svc_name, policy in condition.routing.items():
        if svc_name not in raw["services"]:
            raise ConfigError(f"condition '{condition.name}': unknown service '{svc_name}'")
        raw["services"][svc_name]["routing"] = policy
    return parse_placement(raw, envs, f"condition '{condition.name}'")


def links_for(condition: Condition, envs: EnvironmentsConfig) -> EnvironmentsConfig:
    merged = dict(envs.link_rtt_ms)
    canonical = {frozenset(k.split("--")): k for k in merged}
    for key, value in condition.links.items():
        merged[canonical.get(frozenset(key.split("--")), key)] = value
    return parse_environments({"environments": envs.model_dump()["environments"], "link_rtt_ms": merged},
                              f"condition '{condition.name}' links")


def validate_against_config(scenario: Scenario, envs: EnvironmentsConfig, base: PlacementConfig) -> None:
    """Every condition must produce a valid placement/link matrix; fault targets must be instances."""
    targets = {name for _s, name, _u in base.identity()}
    for c in scenario.conditions:
        placement_for(c, base, envs)
        links_for(c, envs)
        for f in c.faults:
            if f.target not in targets:
                raise ConfigError(f"condition '{c.name}': fault target '{f.target}' must be one of {sorted(targets)}")


def load_scenario(path: str | Path, envs: EnvironmentsConfig | None = None,
                  base: PlacementConfig | None = None) -> tuple[Scenario, dict]:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read file ({exc.strerror})") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML ({exc})") from None
    return parse_scenario(raw, str(p), envs, base), raw


def parse_scenario(raw: Any, source: str, envs: EnvironmentsConfig | None = None,
                   base: PlacementConfig | None = None) -> Scenario:
    try:
        scenario = Scenario.model_validate(raw)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
            parts.append(f"{loc}: {err.get('msg', '').removeprefix('Value error, ')}")
        raise ConfigError(f"{source}: " + "; ".join(parts)) from None
    if envs is not None and base is not None:
        try:
            validate_against_config(scenario, envs, base)
        except ConfigError as exc:
            raise ConfigError(f"{source}: {exc}") from None
    return scenario
