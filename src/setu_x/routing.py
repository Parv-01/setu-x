"""Deterministic routing policies with an observable decision log.

Policies (per service, from placement.yaml):
  primary-fallback  the enabled primary; if the primary is disabled, the first enabled secondary
                    (file order). Candidates for Phase 4 fallback: primary, then secondaries.
  round-robin       enabled instances in file order, cycling.
  weighted          smooth weighted round-robin over enabled instances with weight > 0
                    (the algorithm used by nginx): deterministic, no randomness, and over any
                    window of sum(weights) picks each instance is chosen exactly `weight` times.

Every choice produces a RoutingDecision (policy, chosen instance, env, whether that env is
simulated, the reason, and the ordered candidate list). The last N decisions are kept in memory.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from setu_x.placement import EnvironmentsConfig, Instance, ServicePlacement


class NoEligibleInstance(Exception):
    pass


@dataclass(frozen=True)
class RoutingDecision:
    seq: int
    ts: str
    service: str
    policy: str
    instance: str
    env: str
    simulated: bool
    url: str
    reason: str
    candidates: tuple[str, ...]
    placement_version: int | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = list(self.candidates)
        return d


class Router:
    def __init__(self, max_decisions: int = 500) -> None:
        self._seq = itertools.count(1)
        self._rr: dict[str, int] = {}
        self._swrr: dict[str, dict[str, int]] = {}
        self.decisions: deque[RoutingDecision] = deque(maxlen=max_decisions)

    def reset_state(self) -> None:
        """Called when the placement changes, so policies restart from a known state."""
        self._rr.clear()
        self._swrr.clear()

    def choose(
        self,
        service: str,
        placement: ServicePlacement,
        envs: EnvironmentsConfig,
        placement_version: int | None = None,
    ) -> RoutingDecision:
        enabled = [i for i in placement.instances if i.enabled]
        if not enabled:
            raise NoEligibleInstance(f"{service}: no enabled instance")
        policy = placement.routing

        if policy == "primary-fallback":
            primary = next(i for i in placement.instances if i.role == "primary")
            ordered = ([primary] if primary.enabled else []) + [i for i in enabled if i.role != "primary"]
            chosen = ordered[0]
            reason = ("primary instance" if chosen is primary
                      else f"primary '{primary.name}' disabled; first enabled secondary")
        elif policy == "round-robin":
            k = self._rr.get(service, 0)
            chosen = enabled[k % len(enabled)]
            self._rr[service] = k + 1
            reason = f"round-robin position {k % len(enabled) + 1} of {len(enabled)} enabled"
            ordered = [chosen] + [i for i in enabled if i is not chosen]
        else:  # weighted
            pool = [i for i in enabled if i.weight > 0]
            if not pool:
                raise NoEligibleInstance(f"{service}: weighted routing has no enabled instance with weight > 0")
            current = self._swrr.setdefault(service, {})
            total = sum(i.weight for i in pool)
            for i in pool:
                current[i.name] = current.get(i.name, 0) + i.weight
            chosen = max(pool, key=lambda i: current[i.name])  # ties: first in file order
            current[chosen.name] -= total
            weights = ", ".join(f"{i.name}={i.weight}" for i in pool)
            reason = f"smooth weighted round-robin ({weights})"
            ordered = [chosen] + [i for i in pool if i is not chosen]

        decision = self._record(service, policy, chosen, envs, reason, ordered, placement_version)
        return decision

    def _record(self, service: str, policy: str, chosen: Instance, envs: EnvironmentsConfig, reason: str,
                ordered: list[Instance], version: int | None) -> RoutingDecision:
        d = RoutingDecision(
            seq=next(self._seq),
            ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            service=service,
            policy=policy,
            instance=chosen.name,
            env=chosen.env,
            simulated=envs.is_simulated(chosen.env),
            url=chosen.url,
            reason=reason,
            candidates=tuple(i.name for i in ordered),
            placement_version=version,
        )
        self.decisions.append(d)
        return d

    def recent(self, limit: int = 50) -> list[dict]:
        return [d.as_dict() for d in list(self.decisions)[-limit:][::-1]]
