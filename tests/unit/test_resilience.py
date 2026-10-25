"""Backoff bounds, circuit-breaker transitions (fake clock), registry and event log."""
import random

import pytest

from setu_x.resilience import CLOSED, HALF_OPEN, OPEN, BreakerRegistry, CircuitBreaker, EventLog, backoff_ms


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.mark.parametrize("retry,cap", [(1, 50), (2, 100), (3, 200), (4, 400), (6, 400)])
def test_backoff_is_full_jitter_within_cap(retry, cap):
    rng = random.Random(1)
    values = [backoff_ms(retry, 50, 400, rng) for _ in range(500)]
    assert all(0 <= v <= cap for v in values)
    assert max(values) > 0.8 * cap and min(values) < 0.2 * cap  # spread over the whole range


def test_backoff_reproducible_with_seed_and_rejects_retry_zero():
    a = [backoff_ms(2, 50, 400, random.Random(42)) for _ in range(3)]
    b = [backoff_ms(2, 50, 400, random.Random(42)) for _ in range(3)]
    assert a == b
    with pytest.raises(ValueError):
        backoff_ms(0, 50, 400, random.Random())


def make(threshold=3, reset_ms=1000):
    clock, events = FakeClock(), EventLog()
    return CircuitBreaker("inventory-a", threshold, reset_ms, events, clock), clock, events


def test_breaker_opens_after_consecutive_failures_only():
    b, _, events = make(threshold=3)
    b.record(False)
    b.record(False)
    b.record(True)  # success resets the count
    b.record(False)
    b.record(False)
    assert b.state == CLOSED
    b.record(False)
    assert b.state == OPEN and b.acquire() is None
    t = events.recent(1, "breaker_transition")[0]
    assert (t["from_state"], t["to_state"]) == ("closed", "open") and "3 consecutive failures" in t["reason"]


def test_breaker_half_open_after_reset_timeout_single_probe():
    b, clock, _ = make(threshold=1, reset_ms=1000)
    b.record(False)
    clock.advance(0.999)
    assert b.acquire() is None
    clock.advance(0.002)
    assert b.acquire() == "probe" and b.state == HALF_OPEN
    assert b.acquire() is None  # second caller rejected while the probe is in flight


def test_probe_success_closes_probe_failure_reopens():
    b, clock, _ = make(threshold=1, reset_ms=100)
    b.record(False)
    clock.advance(0.2)
    assert b.acquire() == "probe"
    b.record(False)
    assert b.state == OPEN
    clock.advance(0.05)
    assert b.acquire() is None  # reset timer restarted at the failed probe
    clock.advance(0.06)
    assert b.acquire() == "probe"
    b.record(True)
    assert b.state == CLOSED and b.acquire() == "normal"
    assert b.transitions == [(CLOSED, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, OPEN), (OPEN, HALF_OPEN),
                             (HALF_OPEN, CLOSED)]


def test_unused_probe_is_released_not_lost():
    b, clock, _ = make(threshold=1, reset_ms=100)
    b.record(False)
    clock.advance(0.2)
    assert b.acquire() == "probe"
    b.release_probe()
    assert b.state == HALF_OPEN and b.acquire() == "probe"


def test_registry_lazy_per_instance_and_reset_on_settings_change():
    events = EventLog()
    reg = BreakerRegistry(events, clock=FakeClock())
    with pytest.raises(RuntimeError):
        reg.get("x")
    reg.configure(2, 500)
    a = reg.get("inventory-a")
    assert reg.get("inventory-a") is a and reg.get("inventory-b") is not a
    reg.configure(2, 500)  # unchanged: same breakers
    assert reg.get("inventory-a") is a
    reg.configure(3, 500)  # changed: fresh breakers
    assert reg.get("inventory-a") is not a and events.count("breakers_reset") == 1
    reg.reset("placement changed")
    assert reg.snapshot() == [] and events.count("breakers_reset") == 2


def test_event_log_bounded_newest_first_and_filter():
    log = EventLog(maxlen=3)
    for i in range(5):
        log.add("retry" if i % 2 else "fallback", i=i)
    recent = log.recent(10)
    assert [e["i"] for e in recent] == [4, 3, 2]
    assert [e["i"] for e in log.recent(10, "retry")] == [3]
    assert recent[0]["ts"].endswith("+00:00")
