# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Asynchronous transport-adapter conformance battery.

The async twin of ``test_sync_contract``. It drives an ``async_harness`` and
covers the facets that are async-specific — pre-dispatch failure surfacing
through the returned awaitable, ``asyncio.CancelledError`` cancellation with a
shielded close cascade — alongside the shared framing / header / status
contract that both pillars must honour.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

import pytest

from dexpace.sdk.core.errors import (
    NetworkError,
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Status

if TYPE_CHECKING:
    from .harness import AsyncTransportHarness

_URL = "http://service.test/v1"


def _get(url: str = _URL, *, timeout: float | None = None) -> Request:
    return Request(method=Method.GET, url=Url.parse(url), timeout=timeout)


def _post(body: RequestBody, url: str = "http://service.test/upload") -> Request:
    return Request(method=Method.POST, url=Url.parse(url), body=body)


async def _execute_error(harness: AsyncTransportHarness, request: Request) -> BaseException:
    """Await ``request`` expecting a failure; return the raised error."""
    client = harness.new_async_client()
    with pytest.raises(SdkError) as excinfo:
        await client.execute(request)
    return excinfo.value


# --------------------------------------------------------------------------- #
# Success shape + redirect/retry inertia.
# --------------------------------------------------------------------------- #


async def test_success_yields_a_response_never_none(async_harness: AsyncTransportHarness) -> None:
    async_harness.expect_response(200, body=b"ok")
    response = await async_harness.new_async_client().execute(_get())
    assert response is not None
    assert isinstance(response, AsyncResponse)
    async with response as resp:
        assert resp.body is not None
        assert await resp.body.bytes() == b"ok"


async def test_adapter_does_not_follow_redirects(async_harness: AsyncTransportHarness) -> None:
    async_harness.expect_response(302, headers=[("Location", "http://service.test/moved")])
    async with await async_harness.new_async_client().execute(_get()) as response:
        assert response.status == Status(302)
    assert async_harness.attempt_count() == 1


async def test_adapter_does_not_retry_on_failure(async_harness: AsyncTransportHarness) -> None:
    async_harness.expect_connect_error()
    await _execute_error(async_harness, _get())
    assert async_harness.attempt_count() == 1


# --------------------------------------------------------------------------- #
# Error mapping + pre-dispatch surfacing.
# --------------------------------------------------------------------------- #


async def test_connection_failure_maps_to_retryable_network_error(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_connect_error()
    err = await _execute_error(async_harness, _get())
    assert isinstance(err, ServiceRequestError)
    assert isinstance(err, NetworkError)
    assert err.retryable is True


async def test_connect_timeout_maps_to_retryable_timeout(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_connect_timeout()
    err = await _execute_error(async_harness, _get())
    assert isinstance(err, ServiceRequestTimeoutError)
    assert err.retryable is True


async def test_read_timeout_maps_to_response_timeout(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_read_timeout()
    err = await _execute_error(async_harness, _get())
    assert isinstance(err, ServiceResponseTimeoutError)


async def test_pre_dispatch_failure_surfaces_through_awaitable(
    async_harness: AsyncTransportHarness,
) -> None:
    # A pre-dispatch failure must not throw synchronously at call time; it rides
    # out on the returned awaitable so `async` call sites see it uniformly.
    client = async_harness.new_async_client()
    await async_harness.aclose_client(client)
    coro = client.execute(_get())
    assert inspect.iscoroutine(coro)
    with pytest.raises(ServiceRequestError):
        await coro


# --------------------------------------------------------------------------- #
# Content-Type resolution + adapter-computed framing.
# --------------------------------------------------------------------------- #


async def test_explicit_content_type_is_authoritative(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(200)
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    request = _post(body).with_header("Content-Type", "text/plain")
    await (await async_harness.new_async_client().execute(request)).close()
    assert async_harness.sent_requests()[0].content_type == "text/plain"


async def test_body_derived_content_type_used_when_absent(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(200)
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    await (await async_harness.new_async_client().execute(_post(body))).close()
    assert async_harness.sent_requests()[0].content_type == "application/json"


async def test_empty_body_bearing_method_sends_zero_length(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(200)
    request = Request(method=Method.POST, url=Url.parse("http://service.test/x"))
    await (await async_harness.new_async_client().execute(request)).close()
    assert async_harness.sent_requests()[0].content_length == "0"


async def test_single_use_body_written_exactly_once(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(200)
    body = RequestBody.from_iter(iter([b"chunk-1;", b"chunk-2"]))
    await (await async_harness.new_async_client().execute(_post(body))).close()
    assert async_harness.sent_requests()[0].body == b"chunk-1;chunk-2"
    with pytest.raises(RuntimeError):
        list(body.iter_bytes())


# --------------------------------------------------------------------------- #
# Header hygiene.
# --------------------------------------------------------------------------- #


async def test_native_rejected_header_dropped_counted_and_logged(
    async_harness: AsyncTransportHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if not async_harness.supports_native_header_rejection:
        pytest.skip("harness cannot inject a native-rejected header")
    async_harness.mark_native_rejected("X-Bad")
    async_harness.expect_response(200)
    request = _get().with_header("X-Bad", "boom").with_header("X-Ok", "fine")
    with caplog.at_level(logging.WARNING):
        await (await async_harness.new_async_client().execute(request)).close()
    sent = async_harness.sent_requests()[0]
    assert sent.headers.get("x-bad") is None
    assert sent.headers.get("x-ok") == "fine"
    assert async_harness.drop_log().count("X-BAD") == 1
    assert any("native-rejected" in message for message in caplog.messages)


async def test_malformed_inbound_header_dropped_obs_text_preserved(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(
        200,
        headers=[("X-Good", "ok"), ("Bad Name", "x"), ("X-Obs", "caf\xe9")],
    )
    async with await async_harness.new_async_client().execute(_get()) as response:
        assert set(response.headers.names()) == {"x-good", "x-obs"}
        assert response.headers.get("x-obs") == "caf\xe9"


# --------------------------------------------------------------------------- #
# Response lifecycle: adaptation failure, close cascade, cancellation.
# --------------------------------------------------------------------------- #


async def test_adaptation_failure_closes_native_response(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response("BAD")
    with pytest.raises(ServiceResponseError):
        await async_harness.new_async_client().execute(_get())
    assert async_harness.native_responses()[-1].closed is True


async def test_streamed_response_close_cascades_to_native(
    async_harness: AsyncTransportHarness,
) -> None:
    if not async_harness.supports_streaming:
        pytest.skip("harness cannot produce a streamed response body")
    async_harness.expect_response(200, stream=True, chunks=[b"x", b"y"])
    response = await async_harness.new_async_client().execute(_get())
    await response.close()
    assert async_harness.native_responses()[-1].closed is True


async def test_cancellation_releases_native_via_shielded_close(
    async_harness: AsyncTransportHarness,
) -> None:
    if not async_harness.supports_streaming:
        pytest.skip("harness cannot produce a streamed response body")
    async_harness.expect_response(200, stream=True, chunks=[b"one"], block_after_chunks=True)
    response = await async_harness.new_async_client().execute(_get())
    assert response.body is not None
    body = response.body

    async def consume() -> None:
        async for _chunk in body.aiter_bytes(3):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)  # let the consumer pull "one" and block for more
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded close in the body iterator releases the native resource even
    # though the consuming task was cancelled mid-stream.
    assert async_harness.native_responses()[-1].closed is True


# --------------------------------------------------------------------------- #
# Concurrency + status totality + ownership + proxy.
# --------------------------------------------------------------------------- #


async def test_concurrent_execute_is_safe(async_harness: AsyncTransportHarness) -> None:
    count = 16
    for _ in range(count):
        async_harness.expect_response(200, body=b"ok")
    client = async_harness.new_async_client()

    async def run() -> int:
        async with await client.execute(_get()) as response:
            assert response.body is not None
            await response.body.bytes()
            return int(response.status)

    results = await asyncio.gather(*[run() for _ in range(count)])
    assert results == [200] * count
    assert async_harness.attempt_count() == count


async def test_out_of_range_status_surfaces_with_body(
    async_harness: AsyncTransportHarness,
) -> None:
    async_harness.expect_response(999, body=b"vendor code")
    async with await async_harness.new_async_client().execute(_get()) as response:
        assert int(response.status) == 999
        assert response.body is not None
        assert await response.body.bytes() == b"vendor code"


async def test_byo_client_survives_adapter_close(async_harness: AsyncTransportHarness) -> None:
    if not async_harness.supports_byo:
        pytest.skip("harness cannot build over a caller-supplied client")
    async_harness.expect_response(200)
    client = async_harness.new_async_client(byo=True)
    await (await client.execute(_get())).close()
    await async_harness.aclose_client(client)
    byo = async_harness.byo_underlying()
    assert byo is not None
    assert byo.closed is False
    await async_harness.aclose_client(client)  # idempotent


async def test_execute_after_close_raises(async_harness: AsyncTransportHarness) -> None:
    client = async_harness.new_async_client()
    await async_harness.aclose_client(client)
    with pytest.raises(ServiceRequestError):
        await client.execute(_get())


async def test_proxy_credentials_never_leak(async_harness: AsyncTransportHarness) -> None:
    if not async_harness.supports_proxy_introspection:
        pytest.skip("harness has no proxy surface")
    client = async_harness.new_async_client(proxy_url="http://user:s3cret@proxy.test:8080")
    assert "s3cret" not in repr(client)


async def test_unsupported_proxy_feature_is_discoverable(
    async_harness: AsyncTransportHarness,
) -> None:
    if not async_harness.supports_proxy_introspection:
        pytest.skip("harness has no proxy surface")
    client = async_harness.new_async_client(
        proxy_url="socks5://user:s3cret@proxy.test:1080",
        proxy_features=frozenset({"socks"}),
    )
    unsupported = async_harness.unsupported_proxy_features(client)
    assert "scheme:socks5" in unsupported
