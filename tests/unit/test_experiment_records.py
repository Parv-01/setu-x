"""Observation rows, classification and on-disk records validation."""
from __future__ import annotations

import json

import pytest

from setu_x.experiments.observation import COLUMNS, classify, details_from_body, validate_rows
from setu_x.experiments.records import RecordWriter, read_csv, read_jsonl, validate_records


def good_row(seq=0, run_id="a-r0", outcome="confirmed", status=200, phase="measure"):
    return {"experiment_id": "e", "condition": "a", "run_index": 0, "run_id": run_id, "seq": seq, "phase": phase,
            "request_id": f"{run_id}-{seq}", "scheduled_offset_s": 1.0 + seq,
            "measure_offset_s": (0.5 + seq) if phase == "measure" else -0.5, "t_send_utc": "2026-01-01T00:00:00Z",
            "send_lag_ms": 0.2, "latency_ms": 12.5, "service_latency_ms": 12.3, "http_status": status,
            "outcome": outcome, "error_type": None, "gateway_total_ms": 11.0, "inventory_ms": 4.0, "pricing_ms": 5.0,
            "inventory_instance": "inventory-a", "inventory_env": "aws-simulated", "pricing_instance": "pricing",
            "pricing_env": "azure-simulated", "fallback_used": False, "retries": 0, "attempts": 2,
            "breaker_rejections": 0, "injected_delay_ms": 0.0, "placement_version": 3}


@pytest.mark.parametrize("status, body, expected", [
    (200, {"status": "confirmed"}, ("confirmed", None)),
    (200, {"status": "degraded"}, ("degraded", None)),
    (409, {"status": "rejected"}, ("rejected", None)),
    (503, {"error": {"code": "dependency_unavailable"}}, ("unavailable", "dependency_unavailable")),
    (500, {"error": {"code": "internal_error"}}, ("error", "http_500")),
    (200, {"status": "weird"}, ("error", "http_200")),
    (None, None, ("client_error", None)),
])
def test_classify(status, body, expected):
    assert classify(status, body) == expected


def test_details_from_order_and_error_bodies():
    order = {"timings_ms": {"total": 20.0, "inventory": 8.0, "pricing": 9.0},
             "dependencies": [{"name": "inventory", "ok": True, "instance": "inventory-b", "env": "gcp-simulated",
                               "fallback_used": True},
                              {"name": "pricing", "ok": True, "instance": "pricing", "env": "azure-simulated"}],
             "resilience": {"retries": 1, "attempts": 3, "breaker_rejections": 0,
                            "injected_delay_ms_critical_path": 60.0},
             "placement": {"version": 4}}
    d = details_from_body(200, order)
    assert d["inventory_instance"] == "inventory-b" and d["fallback_used"] is True
    assert d["pricing_env"] == "azure-simulated" and d["injected_delay_ms"] == 60.0 and d["placement_version"] == 4
    err = {"error": {"code": "dependency_unavailable", "details": {
        "timings_ms": {"total": 800.0}, "dependencies": [{"name": "inventory", "ok": False, "instance": "inventory-a",
                                                         "fallback_used": False}],
        "resilience": {"retries": 0, "attempts": 1}, "placement": {"version": 4}}}}
    d = details_from_body(503, err)
    assert d["gateway_total_ms"] == 800.0 and d["inventory_instance"] is None and d["attempts"] == 1
    assert details_from_body(500, "not json") == {}


def test_observation_schema_flags_inconsistent_rows():
    bad_status = good_row(1, outcome="confirmed", status=503)
    no_latency = {**good_row(2), "latency_ms": None}
    phase_mismatch = {**good_row(3), "measure_offset_s": -1.0}
    dropped_with_status = {**good_row(4), "outcome": "dropped"}
    valid, flagged = validate_rows([good_row(0), bad_status, no_latency, phase_mismatch, dropped_with_status])
    assert len(valid) == 1 and {f["seq"] for f in flagged} == {1, 2, 3, 4}


def write(tmp_path, rows):
    d = tmp_path / "exp"
    w = RecordWriter(d)
    w.write_rows(rows)
    w.write_events([{"run_id": "a-r0", "type": "x"}])
    w.close()
    return d


def test_records_roundtrip_and_validate(tmp_path):
    rows = [good_row(i) for i in range(3)] + [good_row(3, phase="warmup")]
    d = write(tmp_path, rows)
    assert read_csv(d / "observations.csv") == read_jsonl(d / "observations.jsonl")
    assert list(read_jsonl(d / "observations.jsonl")[0]) == COLUMNS
    valid, report = validate_records(d, [{"run_id": "a-r0", "scheduled": 4}])
    assert report["ok"] and report["rows_valid"] == 4 and report["problems"] == []


def test_validation_detects_count_mismatch_and_unknown_runs(tmp_path):
    d = write(tmp_path, [good_row(0), good_row(1, run_id="ghost-r0")])
    _, report = validate_records(d, [{"run_id": "a-r0", "scheduled": 2}])
    assert not report["ok"]
    assert any("1 rows on disk, 2 scheduled" in p for p in report["problems"])
    assert any("not listed" in p for p in report["problems"])


def test_validation_detects_duplicates_and_file_disagreement(tmp_path):
    rows = [good_row(0), good_row(1)]
    rows[1]["request_id"] = rows[0]["request_id"]
    d = write(tmp_path, rows)
    lines = (d / "observations.jsonl").read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["latency_ms"] = 1.0
    (d / "observations.jsonl").write_text("\n".join([json.dumps(tampered)] + lines[1:]) + "\n")
    _, report = validate_records(d, [{"run_id": "a-r0", "scheduled": 2}])
    assert any("duplicate request_id" in p for p in report["problems"])
    assert any("differ between" in p for p in report["problems"])


def test_writer_refuses_existing_directory(tmp_path):
    (tmp_path / "exp").mkdir()
    with pytest.raises(FileExistsError):
        RecordWriter(tmp_path / "exp")
