"""One row per scheduled request, and how it is derived from the gateway response.

Columns (also the CSV header, in this order) are documented in docs/methodology.md §3.

Outcome classes:
    confirmed  HTTP 200, status "confirmed"         } ok        (complete, correct response)
    rejected   HTTP 409, insufficient stock         }
    degraded   HTTP 200, status "degraded"          (available, but with a list-price fallback)
    unavailable  HTTP 503 from the gateway          } failed
    error        any other HTTP status              }
    client_timeout   no response within the generator's request timeout
    client_error     connection/transport error at the generator
    dropped          not sent: the generator's in-flight cap was reached (counted, never hidden)

latency_ms is measured from the SCHEDULED send time (includes any generator lag), which is the
latency an open-loop client would experience; service_latency_ms is from the ACTUAL send time.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Outcome = Literal["confirmed", "rejected", "degraded", "unavailable", "error", "client_timeout", "client_error",
                  "dropped"]
OK_OUTCOMES = frozenset({"confirmed", "rejected"})
AVAILABLE_OUTCOMES = OK_OUTCOMES | {"degraded"}
FAILED_OUTCOMES = frozenset({"unavailable", "error", "client_timeout", "client_error"})

COLUMNS = [
    "experiment_id", "condition", "run_index", "run_id", "seq", "phase", "request_id",
    "scheduled_offset_s", "measure_offset_s", "t_send_utc", "send_lag_ms", "latency_ms", "service_latency_ms",
    "http_status", "outcome", "error_type", "gateway_total_ms", "inventory_ms", "pricing_ms",
    "inventory_instance", "inventory_env", "pricing_instance", "pricing_env", "fallback_used", "retries",
    "attempts", "breaker_rejections", "injected_delay_ms", "placement_version",
]


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str
    condition: str
    run_index: int = Field(ge=0)
    run_id: str
    seq: int = Field(ge=0)
    phase: Literal["warmup", "measure"]
    request_id: str
    scheduled_offset_s: float = Field(ge=0)
    measure_offset_s: float
    t_send_utc: str | None
    send_lag_ms: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    service_latency_ms: float | None = Field(default=None, ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)
    outcome: Outcome
    error_type: str | None = None
    gateway_total_ms: float | None = Field(default=None, ge=0)
    inventory_ms: float | None = Field(default=None, ge=0)
    pricing_ms: float | None = Field(default=None, ge=0)
    inventory_instance: str | None = None
    inventory_env: str | None = None
    pricing_instance: str | None = None
    pricing_env: str | None = None
    fallback_used: bool | None = None
    retries: int | None = Field(default=None, ge=0)
    attempts: int | None = Field(default=None, ge=0)
    breaker_rejections: int | None = Field(default=None, ge=0)
    injected_delay_ms: float | None = Field(default=None, ge=0)
    placement_version: int | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Observation:
        if self.outcome == "dropped":
            if self.http_status is not None or self.latency_ms is not None:
                raise ValueError("dropped requests have no status or latency")
            return self
        if self.latency_ms is None or self.t_send_utc is None:
            raise ValueError("sent requests need t_send_utc and latency_ms")
        expected_status = {"confirmed": {200}, "degraded": {200}, "rejected": {409}, "unavailable": {503}}
        if self.outcome in expected_status and self.http_status not in expected_status[self.outcome]:
            raise ValueError(f"outcome '{self.outcome}' with HTTP {self.http_status}")
        if self.outcome in ("client_timeout", "client_error") and self.http_status is not None:
            raise ValueError("client-side failures have no HTTP status")
        if (self.phase == "measure") != (self.measure_offset_s >= 0):
            raise ValueError("phase does not match measure_offset_s")
        return self


def classify(status: int | None, body: Any) -> tuple[str, str | None]:
    """(outcome, error_type) from an HTTP status and parsed JSON body."""
    if status is None:
        return "client_error", None
    if status == 200 and isinstance(body, dict) and body.get("status") in ("confirmed", "degraded"):
        return body["status"], None
    if status == 409 and isinstance(body, dict) and body.get("status") == "rejected":
        return "rejected", None
    if status == 503:
        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        return "unavailable", err.get("code")
    return "error", f"http_{status}"


def details_from_body(status: int | None, body: Any) -> dict[str, Any]:
    """Gateway-reported fields; works for order bodies (200/409) and 503 error envelopes."""
    if not isinstance(body, dict):
        return {}
    src = body
    if status == 503:
        src = (body.get("error") or {}).get("details") or {}
    out: dict[str, Any] = {}
    timings = src.get("timings_ms") or {}
    out["gateway_total_ms"] = timings.get("total")
    out["inventory_ms"] = timings.get("inventory")
    out["pricing_ms"] = timings.get("pricing")
    deps = src.get("dependencies") or []
    inv = next((d for d in deps if d.get("name") == "inventory"), None)
    if inv:
        out["inventory_instance"] = inv.get("instance") if inv.get("ok") else None
        out["inventory_env"] = inv.get("env") if inv.get("ok") else None
        out["fallback_used"] = inv.get("fallback_used")
    pri = next((d for d in deps if d.get("name") == "pricing"), None)
    if pri and pri.get("ok"):
        out["pricing_instance"] = pri.get("instance")
        out["pricing_env"] = pri.get("env")
    res = src.get("resilience") or {}
    out["retries"] = res.get("retries")
    out["attempts"] = res.get("attempts")
    out["breaker_rejections"] = res.get("breaker_rejections")
    out["injected_delay_ms"] = res.get("injected_delay_ms_critical_path")
    out["placement_version"] = (src.get("placement") or {}).get("version")
    return out


def validate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into (valid, flagged). Flagged rows keep the reason and are excluded from metrics."""
    valid, flagged = [], []
    for row in rows:
        try:
            Observation.model_validate(row)
            valid.append(row)
        except ValidationError as exc:
            flagged.append({"seq": row.get("seq"), "run_id": row.get("run_id"),
                            "reason": "; ".join(e["msg"] for e in exc.errors())})
    return valid, flagged
