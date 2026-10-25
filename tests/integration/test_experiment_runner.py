"""The experiment runner end to end against a full in-process stack (real time, short runs)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import yaml

from setu_x.experiments.records import FILES, read_jsonl
from setu_x.experiments.runner import ControlPlane, ExperimentError, ExperimentRunner
from setu_x.experiments.scenario import parse_scenario
from tests.conftest import build_config

ENVS, BASE = build_config()


def scenario(raw: dict):
    text = yaml.safe_dump(raw)
    return parse_scenario(raw, "test", ENVS, BASE), text


def runner_for(stack, raw, tmp_path, load_client=None):
    sc, text = scenario(raw)
    return ExperimentRunner(sc, text, "test.yaml", ENVS, BASE,
                            ControlPlane(stack.client, stack.controller_url, stack.gateway_url),
                            load_client or stack.client, tmp_path, git_commit="abc1234")


SMOKE = {
    "id": "itest", "title": "integration", "workload": {"rate_rps": 20, "warmup_s": 0.5, "duration_s": 4.0},
    "runs": 2, "order": "interleaved", "seed": 3, "cooldown_s": 0, "recovery_hold_s": 0.5,
    "defaults": {"resilience": {"fallback": False, "timeout_ms": 300, "outer_deadline_ms": 1000}},
    "conditions": [
        {"name": "healthy"},
        {"name": "fault", "faults": [{"target": "inventory-a", "mode": "error", "at_s": 0.8, "duration_s": 1}]},
    ],
}


async def test_full_experiment_records_and_summary(make_stack, tmp_path):
    # long poll interval: the gateway only changes placement when the runner syncs it explicitly
    async with make_stack(poll_interval_ms=60_000) as stack:
        r = runner_for(stack, SMOKE, tmp_path)
        summary = await r.run()
        assert r.status == "completed", r.error
        files = {p.name for p in r.dir.iterdir()}
        assert set(FILES) <= files

        v = summary["validation"]
        assert v["ok"], v
        assert v["rows_jsonl"] == v["rows_csv"] == v["rows_scheduled"] == 4 * 90

        manifest = json.loads((r.dir / "manifest.json").read_text())
        assert manifest["status"] == "completed" and manifest["git_commit"] == "abc1234"
        assert manifest["environment_mode"] == "local"
        assert [x["condition"] for x in manifest["run_order"]] == ["healthy", "fault", "healthy", "fault"]
        assert len(manifest["runs"]) == 4 and all(run["scheduled"] == 90 for run in manifest["runs"])

        per_run = {m["run_id"]: m for m in summary["per_run"]}
        for i in (0, 1):
            healthy = per_run[f"healthy-r{i}"]
            assert healthy["success_rate"] == 1.0 and healthy["measure_rows"] == 80
            fault = per_run[f"fault-r{i}"]
            assert fault["outcomes"].get("unavailable", 0) >= 15  # ~20 req/s x 1 s fault, no fallback
            rec = fault["recovery"][0]
            assert rec["impacted"] is True and rec["recovery_s"] is not None and rec["recovery_s"] < 1.2

        # failures happen only inside the fault window (plus scheduling slack)
        rows = read_jsonl(r.dir / "observations.jsonl")
        failed = [x["measure_offset_s"] for x in rows if x["outcome"] == "unavailable"]
        assert failed and min(failed) >= 0.75 and max(failed) <= 1.9
        assert all(x["placement_version"] is not None for x in rows if x["outcome"] == "confirmed")

        events = read_jsonl(r.dir / "events.jsonl")
        injected = [e for e in events if e["source"] == "controller" and e["type"] == "fault_injected"]
        assert len(injected) == 2 and {e["run_id"] for e in injected} == {"fault-r0", "fault-r1"}
        assert any(e["type"] == "fault_scheduled" and e["ok"] for e in events)

        cond = summary["conditions"]["healthy"]
        assert cond["runs"] == 2 and cond["run_level"]["success_rate"]["per_run"] == [1.0, 1.0]
        assert summary["comparisons"]["reference"] == "healthy"

        # platform restored afterwards, including the gateway (immediately, not at its next poll)
        snap = (await stack.client.get("http://controller/api/placement")).json()
        assert snap["source"] == "file"
        cur = (await stack.client.get("http://gateway/api/placement/current")).json()
        assert cur["placement_version"] == snap["version"] and cur["placement_sha256"] == BASE.fingerprint()
        faults = (await stack.client.get("http://controller/api/faults")).json()["faults"]
        assert all(f["active"] is None for f in faults)


async def test_each_run_starts_from_the_file_placement(make_stack, tmp_path):
    raw = {**SMOKE, "runs": 1, "order": "blocked",
           "workload": {"rate_rps": 10, "warmup_s": 0, "duration_s": 0.5},
           "defaults": {},
           "conditions": [{"name": "changed", "call_mode": "sequential", "resilience": {"retries": 2}},
                          {"name": "default"}]}
    async with make_stack() as stack:
        r = runner_for(stack, raw, tmp_path)
        await r.run()
        assert r.status == "completed", r.error
        runs = {x["condition"]: x for x in r.runs}
        assert runs["default"]["placement_sha256"] == BASE.fingerprint()
        assert runs["changed"]["placement_sha256"] != BASE.fingerprint()
        assert runs["default"]["gateway_reset"]["placement_sha256"] == BASE.fingerprint()
        rows = read_jsonl(r.dir / "observations.jsonl")
        by = {c: {x["placement_version"] for x in rows if x["condition"] == c} for c in runs}
        assert by["changed"] == {runs["changed"]["placement_version"]}
        assert by["default"] == {runs["default"]["placement_version"]}


async def test_mode_mismatch_refused_before_anything_is_written(make_stack, tmp_path):
    raw = {**SMOKE, "requires_mode": "simulated"}
    async with make_stack(mode="local") as stack:
        r = runner_for(stack, raw, tmp_path)
        with pytest.raises(ExperimentError, match="SETU_MODE=simulated"):
            await r.run()
        assert not r.dir.exists()


async def test_simulated_injected_delay_is_measured(make_stack, tmp_path):
    zero = {k: 0 for k in ENVS.link_rtt_ms}
    raw = {"id": "sim", "title": "sim", "requires_mode": "simulated", "runs": 1, "order": "blocked", "seed": 1,
           "cooldown_s": 0, "workload": {"rate_rps": 10, "warmup_s": 0.3, "duration_s": 1.5},
           "defaults": {"call_mode": "sequential", "links": zero},
           "conditions": [{"name": "d0"}, {"name": "d100", "links": {"local--azure-simulated": 100}}]}
    async with make_stack(mode="simulated") as stack:
        r = runner_for(stack, raw, tmp_path)
        summary = await r.run()
        assert r.status == "completed", r.error
        med = {c: s["run_level"]["median_latency_ok_ms"]["median"] for c, s in summary["conditions"].items()}
        assert 85 <= med["d100"] - med["d0"] <= 130
        inj = {m["condition"]: m["injected_delay_ms_mean"] for m in summary["per_run"]}
        assert inj == {"d0": 0.0, "d100": 100.0}


async def test_stop_keeps_completed_runs(make_stack, tmp_path):
    raw = {**SMOKE, "runs": 3, "workload": {"rate_rps": 10, "warmup_s": 0, "duration_s": 0.6},
           "conditions": [{"name": "healthy"}]}
    async with make_stack() as stack:
        r = runner_for(stack, raw, tmp_path)
        task = asyncio.create_task(r.run())
        while len(r.runs) < 1:
            await asyncio.sleep(0.05)
        task.cancel()
        summary = await task
        assert r.status == "stopped"
        assert 1 <= summary["runs_completed"] < 3
        assert summary["validation"]["ok"], summary["validation"]
        assert any("stopped" in w for w in summary["warnings"])


async def test_failed_fault_injection_is_reported(make_stack, tmp_path):
    async with make_stack() as stack:
        del stack.router.routes["inventory-a"]  # the controller cannot reach the fault target
        raw = {**SMOKE, "runs": 1, "conditions": [SMOKE["conditions"][1]]}
        r = runner_for(stack, raw, tmp_path)
        with pytest.raises(ExperimentError):
            await r.control.clear_faults()
        r.control.clear_faults = _noop  # type: ignore[method-assign]
        summary = await r.run()
        assert any("fault injection(s) failed" in w for w in summary["warnings"])


async def _noop() -> None:
    return None


async def test_events_endpoint_since_and_wrap_detection():
    # gateway/controller event logs support ?since=; the control plane reports wrapped logs
    gw = httpx.MockTransport(lambda req: httpx.Response(200, json={"events": [], "last_seq": 50, "oldest_seq": 40}))
    async with httpx.AsyncClient(transport=gw) as http:
        cp = ControlPlane(http, "http://controller", "http://gateway")
        _g, _c, warnings = await cp.events_since(10, 45)
        assert any("gateway event log wrapped" in w for w in warnings)
        assert not any("controller" in w for w in warnings)


async def test_final_status_only_after_summary_is_written(make_stack, tmp_path):
    # regression: GET /api/experiments/{id} once returned "completed" before summary.json existed
    raw = {**SMOKE, "runs": 1, "workload": {"rate_rps": 5, "warmup_s": 0, "duration_s": 0.4},
           "conditions": [{"name": "healthy"}]}
    async with make_stack() as stack:
        r = runner_for(stack, raw, tmp_path)
        seen = {}
        original = r.analyse

        def spy(status):
            seen["during"] = r.status
            seen["summary_exists_before"] = (r.dir / "summary.json").exists()
            return original(status)

        r.analyse = spy  # type: ignore[method-assign]
        await r.run()
        assert seen == {"during": "analysing", "summary_exists_before": False}
        assert r.status == "completed" and (r.dir / "summary.json").exists()
