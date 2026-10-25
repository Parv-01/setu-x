"""Scenario files, schedules and run order."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from setu_x.common.config import ConfigError
from setu_x.experiments.scenario import Scenario, links_for, load_scenario, parse_scenario, placement_for
from setu_x.experiments.schedule import arrival_offsets, run_order, run_seed
from tests.conftest import CONFIG_DIR, build_config

SCENARIOS = sorted((CONFIG_DIR / "scenarios").glob("*.yaml"))
ENVS, BASE = build_config()

MINIMAL = {"id": "t", "title": "t", "workload": {"rate_rps": 10, "duration_s": 5}, "conditions": [{"name": "a"}]}


def make(**changes):
    raw = copy.deepcopy(MINIMAL)
    raw.update(changes)
    return raw


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_shipped_scenarios_are_valid(path: Path):
    sc, raw = load_scenario(path, ENVS, BASE)
    assert sc.id and sc.title and raw
    for c in sc.conditions:
        placement_for(c, BASE, ENVS)


def test_expected_scenarios_exist():
    names = {p.stem for p in SCENARIOS}
    assert {"A_baseline", "B_network_delay", "C_service_failure", "D_policies_hang", "D_retries_transient",
            "E_scaling", "F_placement", "G_hybrid_fallback", "smoke", "smoke_simulated"} <= names


@pytest.mark.parametrize("raw, message", [
    (make(workload={"rate_rps": 101, "duration_s": 5}), "rate_rps"),
    (make(workload={"rate_rps": 10, "duration_s": 301}), "duration_s"),
    (make(runs=20, workload={"rate_rps": 10, "duration_s": 300}, conditions=[{"name": "a"}, {"name": "b"}]),
     "limit is"),
    (make(conditions=[{"name": "a"}, {"name": "a"}]), "unique"),
    (make(conditions=[{"name": "a", "faults": [{"target": "inventory-a", "mode": "error", "at_s": 5,
                                                 "duration_s": 2}]}]), "measurement window"),
    (make(conditions=[{"name": "a", "links": {"local--aws-simulated": 5}}]), "simulated"),
    (make(conditions=[{"name": "a", "faults": [{"target": "inventory-a", "mode": "slow", "at_s": 1,
                                                 "duration_s": 2}]}]), "delay_ms"),
    (make(conditions=[{"name": "a", "faults": [{"target": "inventory-a", "mode": "error", "at_s": 1,
                                                 "duration_s": 999}]}]), "duration_s"),
    (make(unknown_field=1), "unknown_field"),
    (make(workload={"rate_rps": 10, "duration_s": 5, "skus": ["BAD"]}), "invalid sku"),
])
def test_scenario_schema_rejects(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_scenario(raw, "test", ENVS, BASE)


@pytest.mark.parametrize("cond, message", [
    ({"name": "a", "instance_env": {"nope": "local"}}, "unknown instance"),
    ({"name": "a", "instance_env": {"pricing": "mars-simulated"}}, "unknown environment"),
    ({"name": "a", "resilience": {"timeout_ms": 5000}}, "outer_deadline_ms"),
    ({"name": "a", "faults": [{"target": "gateway", "mode": "error", "at_s": 1, "duration_s": 1}]}, "fault target"),
    ({"name": "a", "instance_enabled": {"inventory-a": False, "inventory-b": False}}, "enabled"),
])
def test_conditions_validated_against_config(cond, message):
    with pytest.raises(ConfigError, match=message):
        parse_scenario(make(conditions=[cond]), "test", ENVS, BASE)


def test_simulated_link_values_validated():
    raw = make(requires_mode="simulated", conditions=[{"name": "a", "links": {"local--aws-simulated": 5000}}])
    with pytest.raises(ConfigError, match="outside"):
        parse_scenario(raw, "test", ENVS, BASE)


def test_defaults_deep_merge_and_condition_wins():
    raw = make(requires_mode="simulated",
               defaults={"call_mode": "sequential", "resilience": {"retries": 1, "backoff": {"base_ms": 10}},
                         "links": {"local--aws-simulated": 7, "local--azure-simulated": 8}},
               conditions=[{"name": "a"},
                           {"name": "b", "call_mode": "parallel", "resilience": {"backoff": {"max_ms": 20}},
                            "links": {"local--azure-simulated": 99}}])
    sc = parse_scenario(raw, "test", ENVS, BASE)
    a, b = sc.conditions
    assert a.call_mode == "sequential" and b.call_mode == "parallel"
    assert b.resilience == {"retries": 1, "backoff": {"base_ms": 10, "max_ms": 20}}
    assert b.links == {"local--aws-simulated": 7, "local--azure-simulated": 99}
    pb = placement_for(b, BASE, ENVS)
    assert pb.resilience.retries == 1 and pb.resilience.backoff.base_ms == 10 and pb.resilience.backoff.max_ms == 20
    lb = links_for(b, ENVS)
    assert lb.rtt_ms("local", "azure-simulated") == 99 and lb.rtt_ms("aws-simulated", "local") == 7
    # merging is idempotent: re-parsing the recorded (merged) scenario gives the same thing
    again = Scenario.model_validate(sc.model_dump())
    assert again.fingerprint() == sc.fingerprint()


def test_placement_for_relabels_and_leaves_base_untouched():
    sc = parse_scenario(make(conditions=[{"name": "a", "instance_env": {"pricing": "local"},
                                          "routing": {"inventory": "round-robin"}}]), "t", ENVS, BASE)
    p = placement_for(sc.conditions[0], BASE, ENVS)
    assert p.services.pricing.instances[0].env == "local"
    assert p.services.inventory.routing == "round-robin"
    assert BASE.services.pricing.instances[0].env == "azure-simulated"
    assert p.identity() == BASE.identity()  # names/URLs never change


def test_rate_override():
    sc = parse_scenario(make(conditions=[{"name": "a"}, {"name": "b", "rate_rps": 40}]), "t", ENVS, BASE)
    assert [sc.rate_for(c) for c in sc.conditions] == [10, 40]


# ----------------------------------------------------------------------------------- schedules
def test_constant_schedule():
    offs = arrival_offsets(20, 2.0, "constant", seed=1)
    assert len(offs) == 40
    assert offs[0] == 0 and offs[1] == pytest.approx(0.05) and offs[-1] < 2.0


def test_poisson_schedule_reproducible_and_plausible():
    a = arrival_offsets(20, 1000, "poisson", seed=5)
    assert a == arrival_offsets(20, 1000, "poisson", seed=5)
    assert a != arrival_offsets(20, 1000, "poisson", seed=6)
    assert all(0 <= x < 1000 for x in a) and a == sorted(a)
    assert abs(len(a) - 20000) < 5 * 20000 ** 0.5  # Poisson count: mean 20000, sd ~141
    gaps = [y - x for x, y in zip(a, a[1:], strict=False)]
    assert sum(gaps) / len(gaps) == pytest.approx(0.05, rel=0.03)


def test_schedule_rejects_unknown_process():
    with pytest.raises(ValueError):
        arrival_offsets(1, 1, "bursty", 0)


def test_run_orders():
    conds = ["a", "b", "c"]
    blocked = run_order(conds, 2, "blocked", 0)
    assert blocked == [("a", 0), ("a", 1), ("b", 0), ("b", 1), ("c", 0), ("c", 1)]
    inter = run_order(conds, 2, "interleaved", 0)
    assert inter[:3] == [("a", 0), ("b", 0), ("c", 0)]
    rnd = run_order(conds, 3, "randomized", 9)
    assert rnd == run_order(conds, 3, "randomized", 9)
    assert sorted(rnd) == sorted(run_order(conds, 3, "blocked", 0))
    assert rnd != run_order(conds, 3, "blocked", 0)


def test_run_seeds_distinct():
    seeds = {run_seed(7, c, r) for c in range(20) for r in range(20)}
    assert len(seeds) == 400
