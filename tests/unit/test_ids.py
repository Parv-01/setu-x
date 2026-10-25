import re

from setu_x.common import ids


def test_new_request_id_is_valid_and_unique():
    a, b = ids.new_request_id(), ids.new_request_id()
    assert a != b and ids.valid_request_id(a)


def test_request_id_validation_rejects_unsafe_values():
    assert not ids.valid_request_id("")
    assert not ids.valid_request_id(None)
    assert not ids.valid_request_id("bad id with spaces")
    assert not ids.valid_request_id("x" * 129)
    assert ids.valid_request_id("client-123_abc.def")


def test_traceparent_roundtrip():
    tid = ids.new_trace_id()
    header = ids.make_traceparent(tid)
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", header)
    assert ids.parse_traceparent(header)[0] == tid


def test_traceparent_rejects_invalid_values():
    bad = [
        None,
        "",
        "garbage",
        "00-" + "0" * 32 + "-" + "a" * 16 + "-01",  # all-zero trace id
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # all-zero parent id
        "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",  # forbidden version
        "00-" + "a" * 31 + "-" + "b" * 16 + "-01",  # wrong length
    ]
    for value in bad:
        assert ids.parse_traceparent(value) is None, value
