"""Resilience building blocks: retry backoff, circuit breaker, bounded event log.

Backoff ("full jitter"): before retry k (k = 1..retries) the caller sleeps
    uniform(0, min(max_ms, base_ms * 2**(k-1))) milliseconds.
Retries are capped at 3 by config validation and never start if the order's remaining deadline is
shorter than the backoff, so one order makes at most (1 + retries) attempts per instance.

Circuit breaker (one per downstream instance), with failure = timeout, connect error, transport
error, HTTP 5xx or malformed response:
    CLOSED    --(consecutive_failures failures in a row)-->            OPEN
    OPEN      --(reset_timeout_ms elapsed; next call is the probe)-->   HALF_OPEN
    HALF_OPEN --(probe succeeds)--> CLOSED
    HALF_OPEN --(probe fails)-->    OPEN
While OPEN, and while a HALF_OPEN probe is in flight, calls are rejected immediately (fail fast).
Every transition is written to the event log with a UTC timestamp.

Clock and random generator are injectable so tests are deterministic.
"""
from __future__ import annotations

import itertools
import random
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

FAILURE_TYPES = frozenset({"timeout", "connect_error", "transport_error", "http_5xx", "bad_response"})
RETRYABLE_TYPES = frozenset({"timeout", "connect_error", "transport_error", "http_5xx"})

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


def backoff_ms(retry_number: int, base_ms: int, max_ms: int, rng: random.Random) -> float:
    """Full-jitter backoff before retry `retry_number` (1-based)."""
    if retry_number < 1:
        raise ValueError("retry_number starts at 1")
    cap = min(max_ms, base_ms * 2 ** (retry_number - 1))
    return rng.uniform(0, cap)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class EventLog:
    """Bounded, append-only list of structured events (newest last)."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = itertools.count(1)

    def add(self, type_: str, **fields: Any) -> dict[str, Any]:
        event = {"seq": next(self._seq), "ts": utc_now(), "type": type_, **fields}
        self._events.append(event)
        return event

    def recent(self, limit: int = 100, type_: str | None = None) -> list[dict[str, Any]]:
        items = [e for e in self._events if type_ is None or e["type"] == type_]
        return items[-limit:][::-1]

    def count(self, type_: str) -> int:
        return sum(1 for e in self._events if e["type"] == type_)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        consecutive_failures: int,
        reset_timeout_ms: int,
        events: EventLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.threshold = consecutive_failures
        self.reset_timeout_s = reset_timeout_ms / 1000.0
        self.events = events
        self.clock = clock
        self.state = CLOSED
        self.failures = 0
        self.opened_at: float | None = None
        self.probe_in_flight = False
        self.transitions: list[tuple[str, str]] = []

    def _transition(self, new: str, reason: str) -> None:
        old, self.state = self.state, new
        self.transitions.append((old, new))
        if self.events is not None:
            self.events.add("breaker_transition", instance=self.name, from_state=old, to_state=new, reason=reason)

    def acquire(self) -> str | None:
        """Ask to send a call. Returns None (rejected), "normal" (breaker closed) or "probe" (the single
        half-open trial call). Moves OPEN -> HALF_OPEN when the reset timeout has elapsed."""
        if self.state == CLOSED:
            return "normal"
        if self.state == OPEN:
            if self.clock() - (self.opened_at or 0.0) >= self.reset_timeout_s:
                self._transition(HALF_OPEN, f"reset timeout {self.reset_timeout_s * 1000:.0f} ms elapsed")
                self.probe_in_flight = True
                return "probe"
            return None
        # HALF_OPEN: only one probe at a time
        if self.probe_in_flight:
            return None
        self.probe_in_flight = True
        return "probe"

    def allow(self) -> bool:
        return self.acquire() is not None

    def record(self, success: bool) -> None:
        if self.state == HALF_OPEN:
            self.probe_in_flight = False
            if success:
                self.failures = 0
                self._transition(CLOSED, "probe succeeded")
            else:
                self.opened_at = self.clock()
                self._transition(OPEN, "probe failed")
            return
        if success:
            self.failures = 0
            return
        self.failures += 1
        if self.state == CLOSED and self.failures >= self.threshold:
            self.opened_at = self.clock()
            self._transition(OPEN, f"{self.failures} consecutive failures (threshold {self.threshold})")

    def release_probe(self) -> None:
        """Give back a HALF_OPEN probe slot that was granted but produced no outcome (e.g. the
        outer deadline had passed, or the request was cancelled). Only the probe holder calls this."""
        if self.state == HALF_OPEN:
            self.probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        return {"instance": self.name, "state": self.state, "consecutive_failures": self.failures,
                "threshold": self.threshold, "reset_timeout_ms": round(self.reset_timeout_s * 1000),
                "transitions": len(self.transitions)}


class BreakerRegistry:
    """One breaker per downstream instance, created lazily with the current settings."""

    def __init__(self, events: EventLog, clock: Callable[[], float] = time.monotonic) -> None:
        self.events = events
        self.clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._settings: tuple[int, int] | None = None

    def configure(self, consecutive_failures: int, reset_timeout_ms: int) -> None:
        new = (consecutive_failures, reset_timeout_ms)
        if new != self._settings:
            if self._breakers:
                self.events.add("breakers_reset", reason="breaker settings changed",
                                consecutive_failures=new[0], reset_timeout_ms=new[1])
            self._breakers.clear()
            self._settings = new

    def reset(self, reason: str) -> None:
        if self._breakers:
            self.events.add("breakers_reset", reason=reason)
        self._breakers.clear()

    def get(self, instance: str) -> CircuitBreaker:
        if self._settings is None:
            raise RuntimeError("BreakerRegistry.configure() must be called first")
        if instance not in self._breakers:
            self._breakers[instance] = CircuitBreaker(instance, *self._settings, events=self.events, clock=self.clock)
        return self._breakers[instance]

    def snapshot(self) -> list[dict[str, Any]]:
        return [b.snapshot() for b in self._breakers.values()]
