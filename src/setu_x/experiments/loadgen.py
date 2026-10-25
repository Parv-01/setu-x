"""Open-loop HTTP load generator.

Requests are issued at pre-computed times (schedule.arrival_offsets) whether or not earlier
requests have finished, so a slow system cannot reduce its own offered load ("coordinated
omission"). A cap on in-flight requests protects the host: when the cap is reached the request is
recorded as `dropped` (never silently skipped). All timing uses time.perf_counter (monotonic); the
generator must run in the same clock domain as the system under test (the `runner` container on the
Compose network), see docs/methodology.md §2.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from setu_x.experiments.observation import classify, details_from_body


@dataclass(frozen=True)
class Planned:
    seq: int
    offset_s: float          # from run start (warm-up included)
    measure_offset_s: float  # from the start of the measurement phase (negative during warm-up)
    phase: str
    request_id: str
    payload: dict[str, Any]


def plan_requests(offsets: list[float], warmup_s: float, id_prefix: str,
                  payloads: Callable[[int], dict]) -> list[Planned]:
    return [Planned(seq=i, offset_s=round(t, 6), measure_offset_s=round(t - warmup_s, 6),
                    phase="warmup" if t < warmup_s else "measure", request_id=f"{id_prefix}-{i:06d}",
                    payload=payloads(i)) for i, t in enumerate(offsets)]


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


async def generate(
    client: httpx.AsyncClient,
    url: str,
    plan: list[Planned],
    *,
    start: float,
    max_in_flight: int,
    timeout_s: float,
    base_row: dict[str, Any],
    clock: Callable[[], float] = time.perf_counter,
    on_progress: Callable[[int], None] | None = None,
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Send `plan` open-loop. `start` is a clock() value (the run's t=0). Returns one row per plan item."""
    rows: list[dict[str, Any] | None] = [None] * len(plan)
    in_flight = 0
    done = 0
    tasks: list[asyncio.Task] = []

    def row(p: Planned, **fields: Any) -> dict[str, Any]:
        return {**base_row, "seq": p.seq, "phase": p.phase, "request_id": p.request_id,
                "scheduled_offset_s": p.offset_s, "measure_offset_s": p.measure_offset_s, **fields}

    async def one(i: int, p: Planned, scheduled: float) -> None:
        nonlocal in_flight, done
        t_send = clock()
        utc = _utc()
        status: int | None = None
        body: Any = None
        error_type: str | None = None
        outcome: str | None = None
        try:
            async with asyncio.timeout(timeout_s):
                # per-request timeout = the scenario's request_timeout_s (overrides the client's default,
                # which would otherwise cut long requests at 5 s; found in the Phase 8 pilot)
                r = await client.post(url, json=p.payload, headers={"X-Request-ID": p.request_id},
                                      timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)))
            status = r.status_code
            try:
                body = r.json()
            except ValueError:
                body = None
        except (TimeoutError, httpx.TimeoutException):
            outcome, error_type = "client_timeout", "client_timeout"
        except httpx.HTTPError as exc:
            outcome, error_type = "client_error", type(exc).__name__
        t_done = clock()
        in_flight -= 1
        if outcome is None:
            outcome, error_type = classify(status, body)
        rows[i] = row(
            p, t_send_utc=utc, send_lag_ms=round(max(0.0, (t_send - scheduled) * 1000), 3),
            latency_ms=round(max(0.0, (t_done - scheduled) * 1000), 3),
            service_latency_ms=round(max(0.0, (t_done - t_send) * 1000), 3),
            http_status=status, outcome=outcome, error_type=error_type, **details_from_body(status, body))
        done += 1
        if on_row:
            on_row(rows[i])
        if on_progress:
            on_progress(done)

    try:
        for i, p in enumerate(plan):
            scheduled = start + p.offset_s
            delay = scheduled - clock()
            if delay > 0:
                await asyncio.sleep(delay)
            if in_flight >= max_in_flight:
                rows[i] = row(p, t_send_utc=None, outcome="dropped", error_type="generator_in_flight_cap")
                done += 1
                if on_row:
                    on_row(rows[i])
                continue
            in_flight += 1
            tasks.append(asyncio.create_task(one(i, p, scheduled)))
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return [r for r in rows if r is not None]
