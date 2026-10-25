"""Experiment records on disk and their validation.

results/<experiment_id>/
    manifest.json        what was run, with what code/config/seeds, where, when, warnings, run list
    observations.jsonl   one JSON object per scheduled request (all phases)
    observations.csv     the same rows, same column order as observation.COLUMNS
    events.jsonl         gateway resilience events + controller events, tagged with run_id
    summary.json         metrics computed by RE-READING the files above, plus validation results

Validation (validate_records) reads both observation files back from disk and checks that they
agree row by row, that request IDs are unique, that every run has exactly as many rows as were
scheduled, and that every row passes the Observation schema. Flagged rows are listed and excluded
from metrics; they are never repaired or dropped silently.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from setu_x.experiments.observation import COLUMNS, validate_rows

FILES = ("manifest.json", "observations.jsonl", "observations.csv", "events.jsonl", "summary.json")

_INT = {"run_index", "seq", "http_status", "retries", "attempts", "breaker_rejections", "placement_version"}
_FLOAT = {"scheduled_offset_s", "measure_offset_s", "send_lag_ms", "latency_ms", "service_latency_ms",
          "gateway_total_ms", "inventory_ms", "pricing_ms", "injected_delay_ms"}
_BOOL = {"fallback_used"}


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


class RecordWriter:
    """Appends rows/events as runs finish, so a stopped experiment keeps every completed run."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=False)
        self._jsonl = (self.dir / "observations.jsonl").open("w", encoding="utf-8", newline="\n")
        self._csv_fh = (self.dir / "observations.csv").open("w", encoding="utf-8", newline="")
        self._csv = csv.DictWriter(self._csv_fh, fieldnames=COLUMNS, extrasaction="raise", lineterminator="\n")
        self._csv.writeheader()
        self._events = (self.dir / "events.jsonl").open("w", encoding="utf-8", newline="\n")

    def write_rows(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            full = {c: r.get(c) for c in COLUMNS}
            self._jsonl.write(json.dumps(full, separators=(",", ":")) + "\n")
            self._csv.writerow({c: ("" if v is None else v) for c, v in full.items()})
        self._jsonl.flush()
        self._csv_fh.flush()

    def write_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            self._events.write(json.dumps(e, separators=(",", ":"), default=str) + "\n")
        self._events.flush()

    def close(self) -> None:
        for fh in (self._jsonl, self._csv_fh, self._events):
            fh.close()


def _convert(col: str, value: str) -> Any:
    if value == "":
        return None
    if col in _INT:
        return int(value)
    if col in _FLOAT:
        return float(value)
    if col in _BOOL:
        if value not in ("True", "False"):
            raise ValueError(f"{col}: expected True/False, got {value!r}")
        return value == "True"
    return value


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path.name}: header does not match the expected columns")
        return [{c: _convert(c, row[c]) for c in COLUMNS} for row in reader]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-9
    return a == b


def validate_records(directory: Path, runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-read observations from disk and validate. Returns (valid rows, validation report).

    `runs`: manifest run entries with run_id and scheduled (rows expected) for completed runs.
    """
    problems: list[str] = []
    jsonl = read_jsonl(directory / "observations.jsonl")
    csv_rows = read_csv(directory / "observations.csv")
    if len(jsonl) != len(csv_rows):
        problems.append(f"row count differs: jsonl {len(jsonl)} vs csv {len(csv_rows)}")
    mismatches = 0
    for a, b in zip(jsonl, csv_rows, strict=False):
        if any(not _same(a.get(c), b.get(c)) for c in COLUMNS):
            mismatches += 1
    if mismatches:
        problems.append(f"{mismatches} rows differ between observations.jsonl and observations.csv")
    ids = Counter(r.get("request_id") for r in jsonl)
    dupes = sorted(k for k, v in ids.items() if v > 1)
    if dupes:
        problems.append(f"{len(dupes)} duplicate request_id values (first: {dupes[0]})")
    per_run = Counter(r.get("run_id") for r in jsonl)
    expected = {r["run_id"]: r["scheduled"] for r in runs}
    for run_id, n in expected.items():
        if per_run.get(run_id, 0) != n:
            problems.append(f"run {run_id}: {per_run.get(run_id, 0)} rows on disk, {n} scheduled")
    unknown = sorted(set(per_run) - set(expected))
    if unknown:
        problems.append(f"rows for runs not listed in the manifest: {unknown}")
    valid, flagged = validate_rows(jsonl)
    report = {
        "rows_jsonl": len(jsonl),
        "rows_csv": len(csv_rows),
        "rows_valid": len(valid),
        "rows_flagged": len(flagged),
        "flagged": flagged[:100],
        "rows_scheduled": sum(expected.values()),
        "problems": problems,
        "ok": not problems and not flagged,
    }
    return valid, report
