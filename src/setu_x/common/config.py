"""Environment-variable configuration with validation.

Every setting is validated at startup. An invalid value stops the service with a message that
names the variable, instead of starting in a half-configured state.
(Phase 3 adds YAML placement/routing config on top of this.)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

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
    inventory_url: str = Field(default="http://inventory-a:8000", pattern=r"^https?://[^\s]+$")
    pricing_url: str = Field(default="http://pricing:8000", pattern=r"^https?://[^\s]+$")
    call_mode: Literal["parallel", "sequential"] = "parallel"
    timeout_ms: int = Field(default=800, ge=10, le=10_000, description="per-attempt timeout")
    connect_timeout_ms: int = Field(default=300, ge=10, le=10_000)
    outer_deadline_ms: int = Field(default=3000, ge=100, le=10_000, description="hard bound per order")

    @model_validator(mode="after")
    def _check_bounds(self) -> GatewaySettings:
        if self.timeout_ms > self.outer_deadline_ms:
            raise ValueError("SETU_TIMEOUT_MS must not exceed SETU_OUTER_DEADLINE_MS")
        return self


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
