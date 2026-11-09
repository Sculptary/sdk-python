import asyncio
import io
import json
from email.utils import formatdate

import anyio
import httpx
import pytest

from sculptary import AsyncSculptary, SculptaryError, _retry


@pytest.fixture
def delays(monkeypatch):
    values = []

    async def sleep(seconds):
        values.append(seconds)

    monkeypatch.setattr(anyio, "sleep", sleep)
    monkeypatch.setattr(_retry.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(_retry, "_clock", lambda: 1_700_000_000)
    return values


def call(handler, method="GET", payload=None, **options):
    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler), **options
        ) as client:
            return await client.request(method, "/fixture", json=payload)

    return asyncio.run(scenario())


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_retry_with_exponential_backoff(status, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={}) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert call(handler, max_retries=2) == {"ok": True}
    assert len(calls) == 3 and delays == [0.5, 1.0]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("network", [False, True])
def test_non_idempotent_requests_are_never_replayed(method, network, delays):
    calls = []

    def handler(request):
        calls.append(request)
        if network:
            raise httpx.ReadError("Fixture response lost")
        return httpx.Response(503, json={})

    with pytest.raises(httpx.ReadError if network else SculptaryError):
        call(handler, method, {"body": "Do this once"})
    assert len(calls) == 1 and not delays


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_idempotent_requests_replay_the_original_encoded_body(method, delays):
    payload, bodies = {"note": "original"}, []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            payload["note"] = "changed while waiting"
            raise httpx.ReadError("Fixture response lost")
        return httpx.Response(200, json={"ok": True})

    assert call(handler, method, payload) == {"ok": True}
    assert bodies == [{"note": "original"}] * 2 and delays == [0.5]


@pytest.mark.parametrize("hint", ["2", formatdate(1_700_000_002, usegmt=True)])
def test_retry_after_seconds_and_http_dates_are_respected(hint, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, json={}, headers={"Retry-After": hint})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    call(handler)
    assert len(calls) == 2 and delays == [2.0]


def test_server_delays_above_the_budget_return_the_original_error(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429, json={"error": {"message": "Wait longer"}}, headers={"Retry-After": "3600"}
        )

    with pytest.raises(SculptaryError, match="Wait longer"):
        call(handler, retry_max_delay=5)
    assert len(calls) == 1 and not delays


def test_zero_retries_and_terminal_statuses_do_not_sleep(delays):
    for status, options in ((503, {"max_retries": 0}), (403, {}), (409, {}), (422, {})):
        calls = []
        with pytest.raises(SculptaryError):
            call(
                lambda request, calls=calls, status=status: (
                    calls.append(request) or httpx.Response(status, json={})
                ),
                **options,
            )
        assert len(calls) == 1
    assert not delays


def test_retry_exhaustion_returns_the_last_error(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": f"Attempt {len(calls)}"}})

    with pytest.raises(SculptaryError, match="Attempt 3"):
        call(handler)
    assert delays == [0.5, 1.0]


def test_one_shot_request_streams_are_not_retried(delays):
    calls = []

    async def scenario():
        async def body():
            yield b"one shot"

        async def handler(request):
            calls.append(request)
            raise httpx.WriteError("Fixture interrupted")

        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.WriteError):
                await client.request("PUT", "/fixture", content=body())

    asyncio.run(scenario())
    assert len(calls) == 1 and not delays


def test_interrupted_path_download_restarts_atomically(tmp_path, delays):
    calls = []

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (1024 * 1024)
            raise httpx.ReadError("Fixture interrupted")

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, stream=Interrupted())
            if len(calls) == 1
            else httpx.Response(200, content=b"complete-model")
        )

    async def scenario():
        dest = tmp_path / "model.glb"
        dest.write_bytes(b"old-model")
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            assert await client.contents.download("alice/shell", "model.glb", dest) == 14
        assert dest.read_bytes() == b"complete-model" and list(tmp_path.iterdir()) == [dest]

    asyncio.run(scenario())
    assert len(calls) == 2 and delays == [0.5]


def test_caller_owned_stream_is_not_replayed_after_output_begins(delays):
    calls = []

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (1024 * 1024)
            raise httpx.ReadError("Fixture interrupted")

    async def scenario():
        buffer = io.BytesIO()
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, stream=Interrupted())
            ),
        ) as client:
            with pytest.raises(httpx.ReadError):
                await client.contents.download("alice/shell", "model.glb", buffer)
        assert len(buffer.getvalue()) == 1024 * 1024 and not buffer.closed

    asyncio.run(scenario())
    assert len(calls) == 1 and not delays


def test_cancelling_backoff_closes_the_response_without_another_attempt():
    async def scenario():
        entered = asyncio.Event()
        calls = []

        async def sleep(seconds):
            entered.set()
            await asyncio.Event().wait()

        original = anyio.sleep
        anyio.sleep = sleep
        try:
            async with AsyncSculptary(
                host="https://x.test",
                token="",
                transport=httpx.MockTransport(
                    lambda request: calls.append(httpx.Response(503, json={})) or calls[-1]
                ),
            ) as client:
                task = asyncio.create_task(client.users.me())
                await asyncio.wait_for(entered.wait(), timeout=2)
                assert calls[0].is_closed
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert client._http.is_closed and len(calls) == 1
        finally:
            anyio.sleep = original

    asyncio.run(scenario())


@pytest.mark.parametrize("hint", ["not a date", "-1", "NaN", "Infinity"])
def test_invalid_retry_after_uses_bounded_backoff(hint, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(503, json={}, headers={"Retry-After": hint})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    call(handler, retry_max_delay=0.1)
    assert delays == [0.1]


@pytest.mark.parametrize(
    "options",
    [
        {"max_retries": -1},
        {"max_retries": True},
        {"max_retries": 1.5},
        {"retry_base_delay": -1},
        {"retry_max_delay": float("inf")},
        {"retry_max_delay": float("nan")},
    ],
)
def test_invalid_retry_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        AsyncSculptary(token="", **options)
