"""Bounded downstream HTTP calls.

Every call has a hard upper bound: min(per-attempt timeout, time left until the order's
outer deadline). The bound is enforced twice: by httpx's own timeouts and by an asyncio
timeout around the whole exchange (httpx read timeouts apply per read, so a dependency that
trickles bytes slowly could otherwise exceed the budget).

Injected link delay (rtt_ms) is reported separately in `injected_delay_ms`, so analyses can
separate configured delay from measured overhead.

Error classification (used in responses, logs and later in experiment exports):
    connect_error      connection refused / DNS failure (e.g. container stopped)
    timeout            no complete response within the bound
    deadline_exceeded  the order's outer deadline had already passed; call not attempted
    http_5xx           dependency answered with a server error
    http_4xx           dependency rejected the request (a gateway bug, not a dependency failure)
    bad_response       non-JSON or malformed body
    transport_error    any other httpx transport error
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class CallResult:
    dependency: str
    url: str
    ok: bool
    status_code: int | None = None
    data: dict[str, Any] | None = None
    error_type: str | None = None
    elapsed_ms: float = 0.0
    served_by: str | None = None
    attempts: int = 0
    injected_delay_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.dependency,
            "ok": self.ok,
            "status_code": self.status_code,
            "error_type": self.error_type,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "served_by": self.served_by,
            "attempts": self.attempts,
            "injected_delay_ms": round(self.injected_delay_ms, 3),
        }


class Deadline:
    def __init__(self, budget_ms: int) -> None:
        self._end = time.monotonic() + budget_ms / 1000.0

    def remaining_s(self) -> float:
        return self._end - time.monotonic()


async def call_json(
    client: httpx.AsyncClient,
    dependency: str,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout_ms: int,
    connect_timeout_ms: int,
    deadline: Deadline,
    rtt_ms: int = 0,
) -> CallResult:
    result = CallResult(dependency=dependency, url=url, ok=False)
    budget_s = min(timeout_ms / 1000.0, deadline.remaining_s())
    if budget_s <= 0:
        result.error_type = "deadline_exceeded"
        return result
    timeout = httpx.Timeout(budget_s, connect=min(connect_timeout_ms / 1000.0, budget_s))
    start = time.perf_counter()
    result.attempts = 1
    try:
        async with asyncio.timeout(budget_s):
            # Simulated link delay (SETU_MODE=simulated): half the configured round trip before the
            # request and half after the response, both inside the same time budget, so a slow
            # "network" counts against the timeout exactly like a real one would.
            if rtt_ms:
                await asyncio.sleep(rtt_ms / 2000.0)
                result.injected_delay_ms += rtt_ms / 2.0
            resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
            if rtt_ms:
                await asyncio.sleep(rtt_ms / 2000.0)
                result.injected_delay_ms += rtt_ms / 2.0
        result.status_code = resp.status_code
        result.served_by = resp.headers.get("x-served-by")
        if resp.status_code >= 500:
            result.error_type = "http_5xx"
        elif resp.status_code >= 400:
            result.error_type = "http_4xx"
        else:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                result.error_type = "bad_response"
            else:
                result.data = body
                result.ok = True
    except (TimeoutError, httpx.TimeoutException):
        result.error_type = "timeout"
    except httpx.ConnectError:
        result.error_type = "connect_error"
    except httpx.TransportError as exc:
        result.error_type = "transport_error"
        result.details["exception"] = type(exc).__name__
    finally:
        result.elapsed_ms = (time.perf_counter() - start) * 1000.0
    return result
