"""Environment-variable configuration with validation.

Every setting is validated at startup. An invalid value stops the service with a message that
names the variable, instead of starting in a half-configured state.
(Phase 3 adds YAML placement/routing config on top of this.)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

ENV_PREFIX = "SETU_"


class ConfigError(ValueError):
    """Raised when configuration is invalid. The message names the offending variable(s)."""


class ServiceSettings(BaseModel):
    service_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    instance: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    env: str = Field(default="local", pattern=r"^[a-z][a-z0-9-]*$")
    mode: Literal["local", "simulated", "cloud"] = "local"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class GatewaySettings(ServiceSettings):
    """Deployment settings only. Placement, routing and timeouts live in configs/placement.yaml."""

    config_dir: str = Field(default="configs", min_length=1)
    controller_url: str = Field(default="http://controller:8000", pattern=r"^(https?://[^\s]+)?$",
                                description="empty string disables controller sync (file config only)")
    poll_interval_ms: int = Field(default=2000, ge=200, le=60_000)
    random_seed: int | None = Field(default=None, ge=0, le=2**32 - 1,
                                    description="seed for retry backoff jitter (reproducible experiments)")


class ControllerSettings(ServiceSettings):
    config_dir: str = Field(default="configs", min_length=1)
    gateway_url: str = Field(default="http://gateway:8000", pattern=r"^https?://[^\s]+$")
    runner_url: str = Field(default="http://runner:8000", pattern=r"^(https?://[^\s]+)?$",
                            description="experiment runner; empty string disables the /api/experiments proxy")
    dashboard_dir: str = Field(default="dashboard", min_length=1)
    allowed_hosts: str = Field(default="127.0.0.1,localhost,::1,controller", min_length=1,
                               description="comma-separated Host header names accepted (DNS-rebinding defence)")


class RunnerSettings(ServiceSettings):
    config_dir: str = Field(default="configs", min_length=1)
    results_dir: str = Field(default="results", min_length=1)
    controller_url: str = Field(default="http://controller:8000", pattern=r"^https?://[^\s]+$")
    gateway_url: str = Field(default="http://gateway:8000", pattern=r"^https?://[^\s]+$")


def _load(model: type[BaseModel], environ: Mapping[str, str], defaults: dict) -> BaseModel:
    data: dict = dict(defaults)
    for name in model.model_fields:
        key = ENV_PREFIX + name.upper()
        if key in environ and environ[key] != "":
            data[name] = environ[key]
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            var = (ENV_PREFIX + str(loc[0]).upper()) if loc else "(settings)"
            problems.append(f"{var}: {err['msg']}")
        raise ConfigError("invalid configuration: " + "; ".join(problems)) from None


def load_service_settings(service_name: str, environ: Mapping[str, str] | None = None) -> ServiceSettings:
    env = os.environ if environ is None else environ
    return _load(ServiceSettings, env, {"service_name": service_name, "instance": service_name})


def load_gateway_settings(environ: Mapping[str, str] | None = None) -> GatewaySettings:
    env = os.environ if environ is None else environ
    return _load(GatewaySettings, env, {"service_name": "gateway", "instance": "gateway"})


def load_controller_settings(environ: Mapping[str, str] | None = None) -> ControllerSettings:
    env = os.environ if environ is None else environ
    return _load(ControllerSettings, env, {"service_name": "controller", "instance": "controller"})


def load_runner_settings(environ: Mapping[str, str] | None = None) -> RunnerSettings:
    env = os.environ if environ is None else environ
    return _load(RunnerSettings, env, {"service_name": "runner", "instance": "runner"})
