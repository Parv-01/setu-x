"""Error classification and time bounds of setu_x.services.gateway.client.call_json."""
import asyncio
import time

import httpx
import pytest

from setu_x.services.gateway.client import Deadline, call_json


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _call(client, *, timeout_ms=200, deadline_ms=1000):
    return await call_json(
        client, "dep", "http://dep/x", {"a": 1}, {}, timeout_ms=timeout_ms, connect_timeout_ms=100,
        deadline=Deadline(deadline_ms),
    )


@pytest.mark.parametrize(
    "handler,expected",
    [
        (lambda r: httpx.Response(200, json={"ok": True}), None),
        (lambda r: httpx.Response(503, json={}), "http_5xx"),
        (lambda r: httpx.Response(404, json={}), "http_4xx"),
        (lambda r: httpx.Response(200, text="not json"), "bad_response"),
        (lambda r: httpx.Response(200, json=[1, 2]), "bad_response"),
    ],
)
async def test_response_classification(handler, expected):
    async with mock_client(handler) as c:
        res = await _call(c)
    assert res.error_type == expected
    assert res.ok is (expected is None)


async def test_connect_error_and_timeout_exception_classification():
    def refuse(r):
        raise httpx.ConnectError("refused", request=r)

    def read_timeout(r):
        raise httpx.ReadTimeout("slow", request=r)

    async with mock_client(refuse) as c:
        assert (await _call(c)).error_type == "connect_error"
    async with mock_client(read_timeout) as c:
        assert (await _call(c)).error_type == "timeout"


async def test_hanging_dependency_is_cut_at_timeout():
    async def hang(r):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    async with mock_client(hang) as c:
        t0 = time.perf_counter()
        res = await _call(c, timeout_ms=150)
        elapsed = time.perf_counter() - t0
    assert res.error_type == "timeout"
    assert elapsed < 0.15 + 0.2, elapsed


async def test_expired_deadline_skips_call():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, json={})

    async with mock_client(handler) as c:
        d = Deadline(1)
        await asyncio.sleep(0.01)
        res = await call_json(c, "dep", "http://dep/x", {}, {}, timeout_ms=200, connect_timeout_ms=100, deadline=d)
    assert res.error_type == "deadline_exceeded" and not calls and res.attempts == 0
