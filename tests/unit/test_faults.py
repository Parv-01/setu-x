"""FaultSpec validation rules and FaultState expiry / reproducibility (fake clock)."""
import pytest
from pydantic import ValidationError

from setu_x.faults import FaultSpec, FaultState
from setu_x.resilience import EventLog
from tests.unit.test_resilience import FakeClock


@pytest.mark.parametrize("spec,fragment", [
    ({"mode": "error"}, "duration_s"),                                   # duration is mandatory
    ({"mode": "error", "duration_s": 0.5}, "duration_s"),
    ({"mode": "error", "duration_s": 301}, "duration_s"),               # bounded to 300 s
    ({"mode": "error", "duration_s": 10, "probability": 1.5}, "probability"),
    ({"mode": "slow", "duration_s": 10}, "requires delay_ms > 0"),
    ({"mode": "slow", "duration_s": 10, "delay_ms": 5001}, "delay_ms"),
    ({"mode": "error", "duration_s": 10, "delay_ms": 100}, "only valid for mode 'slow'"),
    ({"mode": "error", "duration_s": 10, "error_status": 418}, "error_status"),
    ({"mode": "explode", "duration_s": 10}, "mode"),
    ({"mode": "error", "duration_s": 10, "extra": 1}, "extra"),
])
def test_fault_spec_rules(spec, fragment):
    with pytest.raises(ValidationError) as exc:
        FaultSpec.model_validate(spec)
    assert fragment in str(exc.value)


def test_fault_expires_by_itself():
    clock = FakeClock()
    st = FaultState(EventLog(), clock)
    st.set(FaultSpec(mode="error", duration_s=5))
    assert st.should_inject()
    clock.advance(4.9)
    assert st.active() is not None
    clock.advance(0.2)
    assert st.active() is None and not st.should_inject()
    assert st.events.recent(1)[0]["type"] == "fault_cleared" and st.events.recent(1)[0]["reason"] == "expired"


def test_probability_with_seed_is_reproducible():
    def run():
        st = FaultState(EventLog(), FakeClock())
        st.set(FaultSpec(mode="error", duration_s=60, probability=0.3, seed=123))
        return [st.should_inject() for _ in range(200)]

    a, b = run(), run()
    assert a == b
    assert 40 <= sum(a) <= 80  # ~30% of 200


@pytest.mark.parametrize("p,expected", [(0.0, 0), (1.0, 50)])
def test_probability_extremes(p, expected):
    st = FaultState(EventLog(), FakeClock())
    st.set(FaultSpec(mode="error", duration_s=60, probability=p))
    assert sum(st.should_inject() for _ in range(50)) == expected
    assert st.describe()["matched"] == 50 and st.describe()["injected"] == expected
