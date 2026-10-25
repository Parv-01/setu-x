"""Request IDs and W3C Trace Context (`traceparent`) helpers.

Format (W3C Trace Context, version 00): 00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>.
All-zero trace or parent IDs are invalid per the specification.
"""
from __future__ import annotations

import re
import secrets
import uuid

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def valid_request_id(value: str | None) -> bool:
    return bool(value) and bool(_REQUEST_ID_RE.match(value))


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """Return (trace_id, parent_span_id) or None if the header is absent or invalid."""
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip().lower())
    if not m:
        return None
    version, trace_id, parent_id, _flags = m.groups()
    if version == "ff" or trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return trace_id, parent_id


def make_traceparent(trace_id: str, span_id: str | None = None, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id or new_span_id()}-{'01' if sampled else '00'}"
