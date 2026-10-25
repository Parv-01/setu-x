"""Routing policies follow their documented, deterministic behaviour."""
from collections import Counter

import pytest

from setu_x.routing import NoEligibleInstance, Router
from tests.conftest import build_config


def inventory(**inv):
    envs, placement = build_config(inventory=inv)
    return envs, placement.services.inventory


def picks(router, envs, svc, n):
    return [router.choose("inventory", svc, envs).instance for _ in range(n)]


def test_primary_fallback_always_primary_with_reason():
    envs, svc = inventory()
    r = Router()
    assert picks(r, envs, svc, 5) == ["inventory-a"] * 5
    d = r.decisions[-1]
    assert d.reason == "primary instance" and d.candidates == ("inventory-a", "inventory-b")
    assert d.env == "aws-simulated" and d.simulated is True and d.policy == "primary-fallback"


def test_primary_disabled_uses_first_enabled_secondary():
    envs, placement = build_config()
    raw = placement.model_dump()
    raw["services"]["inventory"]["instances"][0]["enabled"] = False
    envs, svc = inventory(instances=raw["services"]["inventory"]["instances"])
    d = Router().choose("inventory", svc, envs)
    assert d.instance == "inventory-b" and "primary 'inventory-a' disabled" in d.reason
    assert d.candidates == ("inventory-b",)


def test_round_robin_cycles_in_file_order():
    envs, svc = inventory(routing="round-robin")
    r = Router()
    assert picks(r, envs, svc, 5) == ["inventory-a", "inventory-b", "inventory-a", "inventory-b", "inventory-a"]
    assert r.decisions[1].reason == "round-robin position 2 of 2 enabled"


def test_weighted_is_exact_over_each_window_and_deterministic():
    envs, placement = build_config()
    insts = placement.model_dump()["services"]["inventory"]["instances"]
    insts[0]["weight"], insts[1]["weight"] = 3, 1
    envs, svc = inventory(routing="weighted", instances=insts)
    seq1 = picks(Router(), envs, svc, 8)
    seq2 = picks(Router(), envs, svc, 8)
    assert seq1 == seq2  # deterministic
    for window in (seq1[:4], seq1[4:]):
        assert Counter(window) == {"inventory-a": 3, "inventory-b": 1}
    # smooth WRR interleaves rather than bursting: never b twice in a row with weights 3:1
    assert "inventory-b,inventory-b" not in ",".join(seq1)


def test_weighted_zero_weight_excluded():
    envs, placement = build_config()
    insts = placement.model_dump()["services"]["inventory"]["instances"]
    insts[1]["weight"] = 0
    envs, svc = inventory(routing="weighted", instances=insts)
    assert set(picks(Router(), envs, svc, 6)) == {"inventory-a"}


def test_reset_state_restarts_round_robin():
    envs, svc = inventory(routing="round-robin")
    r = Router()
    picks(r, envs, svc, 1)
    r.reset_state()
    assert picks(r, envs, svc, 1) == ["inventory-a"]


def test_no_eligible_instance_raises():
    envs, svc = inventory()
    svc = svc.model_copy(update={"instances": [i.model_copy(update={"enabled": False}) for i in svc.instances]})
    with pytest.raises(NoEligibleInstance):
        Router().choose("inventory", svc, envs)


def test_decision_log_is_bounded_and_newest_first():
    envs, svc = inventory(routing="round-robin")
    r = Router(max_decisions=3)
    picks(r, envs, svc, 5)
    recent = r.recent(10)
    assert len(recent) == 3 and [d["seq"] for d in recent] == [5, 4, 3]
