"""Provider adapters behind one provider-neutral interface (see adapters/base.py)."""
from __future__ import annotations

from collections.abc import Mapping

import httpx

from setu_x.adapters.base import AdapterConfigError, CloudAdapter
from setu_x.adapters.cloud import AwsAdapter, AzureAdapter, GcpAdapter
from setu_x.adapters.local_docker import LocalDockerAdapter
from setu_x.placement import EnvironmentsConfig, PlacementConfig

__all__ = ["AwsAdapter", "AzureAdapter", "GcpAdapter", "LocalDockerAdapter", "build_adapters"]


def build_adapters(
    envs: EnvironmentsConfig,
    placement: PlacementConfig,
    client: httpx.AsyncClient,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, CloudAdapter], dict[str, str]]:
    """Return (adapters by name, config errors by adapter name).

    A real-provider adapter with invalid configuration is reported, not silently dropped, and does
    not prevent the local platform from starting.
    """
    adapters: dict[str, CloudAdapter] = {"local-docker": LocalDockerAdapter(envs, placement, client, environ)}
    errors: dict[str, str] = {}
    for cls in (AwsAdapter, AzureAdapter, GcpAdapter):
        try:
            adapters[cls.name] = cls(environ)
        except AdapterConfigError as exc:
            errors[cls.name] = str(exc)
    return adapters, errors
