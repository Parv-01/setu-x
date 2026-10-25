"""Gateway-side placement state: start from the config files, then follow the controller.

- At startup the gateway loads configs/environments.yaml + placement.yaml. Invalid files stop the
  gateway (fail fast, clear message).
- Every poll interval it fetches GET {controller}/api/placement, re-validates it with the same
  models, and applies it if its fingerprint differs (routing state is then reset).
- The controller response also carries the current inter-environment link matrix (link_rtt_ms),
  which experiments can change at runtime; it is validated with the same environment rules.
- Listeners registered in `on_change` are called after a change is applied (the gateway resets
  its circuit breakers and routing state).
- If the controller is unreachable or returns an invalid placement, the gateway keeps the last
  valid placement. `info()` reports which source is in use and whether the controller is
  reachable, so loss of the control plane is visible but not fatal.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from setu_x.common.config import ConfigError
from setu_x.placement import EnvironmentsConfig, PlacementConfig, parse_environments, parse_placement
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
        self.on_change: list[Callable[[str], None]] = []

    def _state_key(self, envs: EnvironmentsConfig, placement: PlacementConfig) -> tuple:
        return placement.fingerprint(), tuple(sorted(envs.link_rtt_ms.items()))

    async def sync_once(self) -> bool:
        """Fetch and apply the controller placement. Returns True if the fetch succeeded."""
        if not self.controller_url or self.client is None:
            return False
        try:
            r = await self.client.get(self.controller_url + "/api/placement", timeout=1.0)
            r.raise_for_status()
            body = r.json()
            envs = self.envs
            if "link_rtt_ms" in body:
                envs = parse_environments({"environments": self.envs.model_dump()["environments"],
                                           "link_rtt_ms": body["link_rtt_ms"]}, "controller link_rtt_ms")
            candidate = parse_placement(body["placement"], envs, "controller /api/placement")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ConfigError) as exc:
            self.controller_reachable = not isinstance(exc, httpx.TransportError)
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("placement_sync_failed", extra={"error": self.last_error, "source_in_use": self.source})
            return False
        self.controller_reachable = True
        self.last_sync_at = _now()
        self.last_error = None
        changed = self._state_key(envs, candidate) != self._state_key(self.envs, self.placement)
        if changed or self.version != body.get("version") or self.source != "controller":
            self.placement = candidate
            self.envs = envs
            self.version = body.get("version")
            self.source = "controller"
            self.applied_at = _now()
            if changed:
                self.router.reset_state()
                for listener in self.on_change:
                    listener("placement or link matrix changed")
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
            "link_rtt_ms": dict(self.envs.link_rtt_ms),
            "placement_applied_at": self.applied_at,
            "controller_url": self.controller_url or None,
            "controller_reachable": self.controller_reachable,
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
        }
