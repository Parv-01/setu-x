"""Bounded, self-expiring fault injection for downstream services (local development only).

A fault affects only business endpoints (routes that declare the returned dependency). Health
endpoints keep answering, so the fault models an application-level failure, not a dead process
(use scripts/fault_container.ps1 to really stop a container).

    error  respond `error_status` (default 503) with probability p
    slow   add `delay_ms` (<= 5000) of server-side processing with probability p
    hang   hold the request until the fault expires or 30 s pass (whichever is first), then 503

Bounds (validated): duration_s in [1, 300] is mandatory; probability in [0, 1]; optional `seed`
makes the probabilistic choice reproducible. Faults auto-expire; a new fault replaces the old one.

Admin API (internal Compose network only; never published to the host):
    POST   /admin/fault   set a fault          -> {"active": {...}}
    GET    /admin/fault   current fault + recent fault events
    DELETE /admin/fault   clear
The controller exposes an allow-listed proxy for these (POST/GET/DELETE /api/faults).
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from setu_x.common.app import error_body
from setu_x.resilience import EventLog, utc_now

MAX_DURATION_S = 300
MAX_HANG_S = 30.0


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["error", "slow", "hang"]
    duration_s: float = Field(ge=1, le=MAX_DURATION_S)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    delay_ms: int = Field(default=0, ge=0, le=5000)
    error_status: Literal[500, 502, 503, 504] = 503
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def _mode_fields(self) -> FaultSpec:
        if self.mode == "slow" and self.delay_ms <= 0:
            raise ValueError("mode 'slow' requires delay_ms > 0")
        if self.mode != "slow" and self.delay_ms:
            raise ValueError("delay_ms is only valid for mode 'slow'")
        return self


class FaultState:
    def __init__(self, events: EventLog, clock: Callable[[], float] = time.monotonic) -> None:
        self.events = events
        self.clock = clock
        self.spec: FaultSpec | None = None
        self.started_at = 0.0
        self.started_utc = ""
        self.rng = random.Random()
        self.matched = 0
        self.injected = 0

    def set(self, spec: FaultSpec) -> dict:
        self.spec = spec
        self.started_at = self.clock()
        self.started_utc = utc_now()
        self.rng = random.Random(spec.seed)
        self.matched = self.injected = 0
        self.events.add("fault_set", **spec.model_dump())
        return self.describe()

    def clear(self, reason: str = "cleared") -> None:
        if self.spec is not None:
            self.events.add("fault_cleared", reason=reason, mode=self.spec.mode,
                            matched=self.matched, injected=self.injected)
        self.spec = None

    def remaining_s(self) -> float:
        if self.spec is None:
            return 0.0
        return self.spec.duration_s - (self.clock() - self.started_at)

    def active(self) -> FaultSpec | None:
        if self.spec is not None and self.remaining_s() <= 0:
            self.clear("expired")
        return self.spec

    def should_inject(self) -> bool:
        spec = self.active()
        if spec is None:
            return False
        self.matched += 1
        hit = self.rng.random() < spec.probability
        if hit:
            self.injected += 1
        return hit

    def describe(self) -> dict | None:
        spec = self.active()
        if spec is None:
            return None
        return {**spec.model_dump(), "started_at": self.started_utc, "remaining_s": round(self.remaining_s(), 3),
                "matched": self.matched, "injected": self.injected}


class InjectedFault(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


def install_fault_injection(app: FastAPI, instance: str, clock: Callable[[], float] = time.monotonic):
    """Register the admin API and return a FastAPI dependency to attach to business routes.

    Implemented as a route dependency (not middleware) so injected responses still pass through
    the request-ID / trace / access-log middleware like any other response.
    """
    events = EventLog(maxlen=200)
    state = FaultState(events, clock)
    app.state.faults = state

    @app.exception_handler(InjectedFault)
    async def _injected(request: Request, exc: InjectedFault):
        return JSONResponse(status_code=exc.status,
                            content=error_body("injected_fault", exc.message,
                                               getattr(request.state, "request_id", None)))

    async def fault_guard() -> None:
        if not state.should_inject():
            return
        spec = state.spec
        assert spec is not None
        if spec.mode == "error":
            raise InjectedFault(spec.error_status, f"injected error on {instance}")
        if spec.mode == "slow":
            await asyncio.sleep(spec.delay_ms / 1000.0)
            return
        await asyncio.sleep(max(0.0, min(state.remaining_s(), MAX_HANG_S)))
        raise InjectedFault(503, f"injected hang on {instance} ended")

    @app.post("/admin/fault", tags=["admin"])
    async def set_fault(spec: FaultSpec) -> dict:
        return {"instance": instance, "active": state.set(spec)}

    @app.get("/admin/fault", tags=["admin"])
    async def get_fault() -> dict:
        return {"instance": instance, "active": state.describe(), "events": events.recent(20)}

    @app.delete("/admin/fault", tags=["admin"])
    async def clear_fault() -> dict:
        state.clear("cleared via API")
        return {"instance": instance, "active": None}

    return fault_guard
