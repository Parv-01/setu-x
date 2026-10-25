"""Gateway-side placement state: start from the config files, then follow the controller.

- At startup the gateway loads configs/environments.yaml + placement.yaml. Invalid files stop the
  gateway (fail fast, clear message).
- Every poll interval it fetches GET {controller}/api/placement, re-validates it with the same
  models, and applies it if its fingerprint differs (routing state is then reset).
- If the controller is unreachable or returns an invalid placement, the gateway keeps the last
  valid placement. `info()` reports which source is in use and whether the controller is
  reachable, so loss of the control plane is visible but not fatal.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from setu_x.common.config import ConfigError
from setu_x.placement import EnvironmentsConfig, PlacementConfig, parse_placement
from setu_x.routing import Router

log = logging.getLogger("setu_x.gateway.placement")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class PlacementSync:
    def __init__(
        self,
        envs: EnvironmentsConfig,
        placement: PlacementConfig,
        router: Router,
        client: httpx.AsyncClient | None = None,
        controller_url: str = "",
        interval_s: float = 2.0,
    ) -> None:
        self.envs = envs
        self.placement = placement
        self.router = router
        self.client = client
        self.controller_url = controller_url.rstrip("/")
        self.interval_s = interval_s
        self.source = "file"
        self.version: int | None = None
        self.applied_at = _now()
        self.controller_reachable: bool | None = None
        self.last_sync_at: str | None = None
        self.last_error: str | None = None

    async def sync_once(self) -> bool:
        """Fetch and apply the controller placement. Returns True if the fetch succeeded."""
        if not self.controller_url or self.client is None:
            return False
        try:
            r = await self.client.get(self.controller_url + "/api/placement", timeout=1.0)
            r.raise_for_status()
            body = r.json()
            candidate = parse_placement(body["placement"], self.envs, "controller /api/placement")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ConfigError) as exc:
            self.controller_reachable = not isinstance(exc, httpx.TransportError)
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("placement_sync_failed", extra={"error": self.last_error, "source_in_use": self.source})
            return False
        self.controller_reachable = True
        self.last_sync_at = _now()
        self.last_error = None
        if candidate.fingerprint() != self.placement.fingerprint() or self.version != body.get("version"):
            changed = candidate.fingerprint() != self.placement.fingerprint()
            self.placement = candidate
            self.version = body.get("version")
            self.source = "controller"
            self.applied_at = _now()
            if changed:
                self.router.reset_state()
                log.info("placement_applied", extra={"version": self.version, "sha256": candidate.fingerprint()})
        return True

    async def run(self) -> None:
        while True:
            await self.sync_once()
            await asyncio.sleep(self.interval_s)

    def info(self) -> dict:
        return {
            "placement_source": self.source,
            "placement_version": self.version,
            "placement_sha256": self.placement.fingerprint(),
            "placement_applied_at": self.applied_at,
            "controller_url": self.controller_url or None,
            "controller_reachable": self.controller_reachable,
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
        }
