"""One logical dependency call with retries, circuit breaking and fallback over routing candidates.

Order of operations for a service call:
  for each candidate instance (the routed instance first; the others only if resilience.fallback):
      if circuit breaking is on and the instance's breaker rejects the call -> record, try next
      attempt 1 .. (1 + retries):
          before a retry: full-jitter backoff, skipped (stop retrying) if the outer deadline is too close
          call with the per-attempt timeout (and simulated link delay in simulated mode)
          success -> done
          non-retryable failure (4xx, malformed, deadline) -> stop retrying this instance
          the breaker opened during this loop -> stop retrying this instance
      move to the next candidate (a fallback), if any
Upper bound on calls for one service per order: (1 + retries) x number of candidates.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from setu_x.placement import EnvironmentsConfig, PlacementConfig
from setu_x.resilience import FAILURE_TYPES, OPEN, RETRYABLE_TYPES, BreakerRegistry, EventLog, backoff_ms
from setu_x.routing import RoutingDecision
from setu_x.services.gateway.client import CallResult, Deadline, call_json


@dataclass
class ServiceOutcome:
    service: str
    ok: bool
    result: CallResult
    instance: str
    env: str
    simulated: bool
    attempts: int = 0
    retries: int = 0
    injected_delay_ms: float = 0.0
    elapsed_ms: float = 0.0
    tried: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    breaker_rejections: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.service,
            "ok": self.ok,
            "status_code": self.result.status_code,
            "error_type": self.result.error_type,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "served_by": self.result.served_by,
            "instance": self.instance,
            "env": self.env,
            "simulated": self.simulated,
            "attempts": self.attempts,
            "retries": self.retries,
            "injected_delay_ms": round(self.injected_delay_ms, 3),
            "fallback_used": self.fallback_used,
            "breaker_rejections": self.breaker_rejections,
            "tried": self.tried,
        }


async def call_with_resilience(
    http: httpx.AsyncClient,
    *,
    service: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    decision: RoutingDecision,
    placement: PlacementConfig,
    envs: EnvironmentsConfig,
    gateway_env: str,
    simulated_mode: bool,
    deadline: Deadline,
    breakers: BreakerRegistry,
    events: EventLog,
    rng: random.Random,
    request_id: str,
) -> ServiceOutcome:
    res = placement.resilience
    cb = res.circuit_breaker
    if cb.enabled:
        breakers.configure(cb.consecutive_failures, cb.reset_timeout_ms)
    by_name = {i.name: i for i in placement.service(service).instances}
    candidates = list(decision.candidates) if res.fallback else [decision.instance]

    start = time.perf_counter()
    attempts = retries = rejections = 0
    injected = 0.0
    tried: list[dict[str, Any]] = []
    last = CallResult(dependency=service, url="", ok=False, error_type="not_attempted")
    last_inst = by_name[decision.instance]

    for idx, name in enumerate(candidates):
        inst = by_name[name]
        last_inst = inst
        if idx > 0:
            events.add("fallback", request_id=request_id, service=service, from_instance=candidates[idx - 1],
                       to_instance=name, reason=tried[-1]["outcome"] if tried else "unknown")
        breaker = breakers.get(name) if cb.enabled else None
        grant = breaker.acquire() if breaker is not None else "normal"
        if grant is None:
            rejections += 1
            tried.append({"instance": name, "outcome": "breaker_open", "attempts": 0})
            events.add("breaker_rejected", request_id=request_id, service=service, instance=name)
            last = CallResult(dependency=service, url=inst.url, ok=False, error_type="breaker_open")
            continue

        rtt = envs.rtt_ms(gateway_env, inst.env) if simulated_mode else 0
        inst_attempts = 0
        try:
            outcome = await _attempts(http, service, path, payload, headers, inst, rtt, res, deadline, breaker,
                                      events, rng, request_id, last)
        finally:
            if breaker is not None and grant == "probe":
                breaker.release_probe()  # no-op if the probe produced an outcome (state already changed)
        last, inst_attempts, inst_retries, inst_injected = outcome
        attempts += inst_attempts
        retries += inst_retries
        injected += inst_injected
        if last.ok:
            elapsed = (time.perf_counter() - start) * 1000.0
            tried.append({"instance": name, "outcome": "ok", "attempts": inst_attempts})
            return ServiceOutcome(service, True, last, name, inst.env, envs.is_simulated(inst.env), attempts,
                                  retries, injected, elapsed, tried, idx > 0, rejections)
        tried.append({"instance": name, "outcome": last.error_type, "attempts": inst_attempts})
        if last.error_type in ("deadline_exceeded", "http_4xx"):
            break  # no time left, or a request the gateway built wrongly: another instance will not help

    elapsed = (time.perf_counter() - start) * 1000.0
    return ServiceOutcome(service, False, last, last_inst.name, last_inst.env, envs.is_simulated(last_inst.env),
                          attempts, retries, injected, elapsed, tried, len(tried) > 1, rejections)


async def _attempts(http, service, path, payload, headers, inst, rtt, res, deadline, breaker, events, rng,
                    request_id, last):
    """Up to (1 + retries) attempts against one instance. Returns (last, attempts, retries, injected_ms)."""
    attempts = retries = 0
    injected = 0.0
    name = inst.name
    for attempt in range(1 + res.retries):
        if attempt > 0:
            wait = backoff_ms(attempt, res.backoff.base_ms, res.backoff.max_ms, rng) / 1000.0
            if deadline.remaining_s() <= wait:
                events.add("retry_skipped", request_id=request_id, instance=name,
                           reason="outer deadline too close for backoff")
                break
            await asyncio.sleep(wait)
            retries += 1
            events.add("retry", request_id=request_id, service=service, instance=name, attempt=attempt + 1,
                       backoff_ms=round(wait * 1000, 3), previous_error=last.error_type)
        last = await call_json(
            http, service, inst.url.rstrip("/") + path, payload, headers,
            timeout_ms=res.timeout_ms, connect_timeout_ms=res.connect_timeout_ms, deadline=deadline, rtt_ms=rtt,
        )
        if last.error_type == "deadline_exceeded":
            break
        attempts += 1
        injected += last.injected_delay_ms
        if breaker is not None:
            breaker.record(success=last.error_type not in FAILURE_TYPES)
        if last.ok:
            break
        if last.error_type not in RETRYABLE_TYPES or (breaker is not None and breaker.state == OPEN):
            break
    return last, attempts, retries, injected
