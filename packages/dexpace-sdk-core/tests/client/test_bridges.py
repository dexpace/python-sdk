# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Sync/async transport bridges (PY-M3-06).

Two adapters cross the sync and async transport pillars:

- ``SyncToAsyncHttpClient`` wraps a blocking ``HttpClient`` for use from an
  async pipeline. It refuses to run without a caller-supplied ``Executor`` — a
  default shared pool is exactly the exhaustible resource that causes
  hard-to-debug starvation, so construction fails immediately without one.
- ``AsyncToSyncHttpClient`` wraps an ``AsyncHttpClient`` for use from a sync
  pipeline, driving it on a dedicated background event-loop thread with clean
  exception unwrapping, cancel-then-re-raise interrupt handling, and
  exactly-once cleanup of orphaned responses.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.client import AsyncToSyncHttpClient, SyncToAsyncHttpClient
from dexpace.sdk.core.client._async_to_sync import _BridgedCall, _unwrap_exception
from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import (
    AsyncResponse,
    AsyncResponseBody,
    Response,
    ResponseBody,
    Status,
)
from dexpace.sdk.core.instrumentation.correlation import get_trace_id, set_trace_id


def _get() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def _sync_response(request: Request, body: ResponseBody | None = None) -> Response:
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        body=body,
    )


def _async_response(request: Request, body: AsyncResponseBody | None = None) -> AsyncResponse:
    return AsyncResponse(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        body=body,
    )


class _CloseTrackingAsyncBody(AsyncResponseBody):
    """An ``AsyncResponseBody`` double that counts (and signals) its closes."""

    def __init__(self, data: bytes = b"payload") -> None:
        self._data = data
        self.close_count = 0
        self._closed = threading.Event()

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            yield self._data

        return _gen()

    async def close(self) -> None:
        self.close_count += 1
        self._closed.set()

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)


# --------------------------------------------------------------------------- #
# SyncToAsyncHttpClient
# --------------------------------------------------------------------------- #


class _RecordingSyncClient(HttpClient):
    """Sync transport double that records the thread and correlation it saw."""

    def __init__(self) -> None:
        self.seen_request: Request | None = None
        self.seen_thread: int | None = None
        self.seen_trace: str | None = None

    def execute(self, request: Request) -> Response:
        self.seen_request = request
        self.seen_thread = threading.get_ident()
        self.seen_trace = get_trace_id()
        return _sync_response(request, ResponseBody.from_bytes(b"hello"))


class TestSyncToAsyncConstruction:
    @pytest.mark.req("SEAM-18")
    def test_missing_executor_raises_at_construction(self) -> None:
        # Omitting the required keyword-only executor fails immediately, before
        # any call — the SDK never fabricates a shared pool.
        with pytest.raises(TypeError):
            SyncToAsyncHttpClient(_RecordingSyncClient())  # type: ignore[call-arg]

    def test_none_executor_raises_at_construction(self) -> None:
        with pytest.raises(TypeError, match="executor"):
            SyncToAsyncHttpClient(_RecordingSyncClient(), executor=None)  # type: ignore[arg-type]


class TestSyncToAsyncExecute:
    @pytest.mark.req("ASYNC-19")
    async def test_runs_on_supplied_executor(self) -> None:
        client = _RecordingSyncClient()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            bridge = SyncToAsyncHttpClient(client, executor=pool)
            request = _get()
            response = await bridge.execute(request)
            assert isinstance(response, AsyncResponse)
            assert response.status is Status.OK
            assert client.seen_request is request  # request threads through unchanged
            assert client.seen_thread is not None
            assert client.seen_thread != threading.get_ident()  # ran off the loop thread
            assert response.body is not None
            assert await response.body.bytes() == b"hello"

    @pytest.mark.req("ASYNC-10", "ASYNC-8", "SEAM-24")
    async def test_contextvars_captured_at_submission(self) -> None:
        client = _RecordingSyncClient()
        token = set_trace_id("trace-sync-to-async")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                bridge = SyncToAsyncHttpClient(client, executor=pool)
                await bridge.execute(_get())
            # The worker observed the correlation id bound at submission, not the
            # bare context a fresh pool thread would otherwise inherit.
            assert client.seen_trace == "trace-sync-to-async"
        finally:
            set_trace_id(None)
            del token


# --------------------------------------------------------------------------- #
# AsyncToSyncHttpClient
# --------------------------------------------------------------------------- #


class _RecordingAsyncClient(AsyncHttpClient):
    """Async transport double that records the thread and correlation it saw."""

    def __init__(self, body: AsyncResponseBody | None = None) -> None:
        self.seen_request: Request | None = None
        self.seen_thread: int | None = None
        self.seen_trace: str | None = None
        self._body = body

    async def execute(self, request: Request) -> AsyncResponse:
        self.seen_request = request
        self.seen_thread = threading.get_ident()
        self.seen_trace = get_trace_id()
        body = self._body if self._body is not None else AsyncResponseBody.from_bytes(b"hi")
        return _async_response(request, body)


class _RaisingAsyncClient(AsyncHttpClient):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute(self, request: Request) -> AsyncResponse:
        raise self._exc


class TestAsyncToSyncExecute:
    @pytest.mark.req("ASYNC-19")
    def test_runs_async_client_on_background_thread(self) -> None:
        client = _RecordingAsyncClient()
        with AsyncToSyncHttpClient(client) as bridge:
            request = _get()
            response = bridge.execute(request)
            assert isinstance(response, Response)
            assert response.status is Status.OK
            assert client.seen_request is request  # threads through unchanged
            assert client.seen_thread is not None
            assert client.seen_thread != threading.get_ident()  # a dedicated thread
            assert response.body is not None
            assert response.body.bytes() == b"hi"

    def test_contextvars_captured_at_submission(self) -> None:
        client = _RecordingAsyncClient()
        token = set_trace_id("trace-async-to-sync")
        try:
            with AsyncToSyncHttpClient(client) as bridge:
                bridge.execute(_get())
            assert client.seen_trace == "trace-async-to-sync"
        finally:
            set_trace_id(None)
            del token

    @pytest.mark.req("ASYNC-14", "SEAM-17", "SEAM-18")
    def test_sdk_exception_surfaces_with_exact_type(self) -> None:
        # A caller catching the specific SDK type must see that exact type, not
        # a generic threading/future wrapper.
        boom = ServiceRequestError("connect failed")
        with AsyncToSyncHttpClient(_RaisingAsyncClient(boom)) as bridge:
            with pytest.raises(ServiceRequestError) as excinfo:
                bridge.execute(_get())
            assert excinfo.value is boom

    @pytest.mark.req("ASYNC-16")
    def test_context_manager_shuts_down_background_thread(self) -> None:
        bridge = AsyncToSyncHttpClient(_RecordingAsyncClient())
        with bridge:
            bridge.execute(_get())
        assert not bridge._loop_bg._thread.is_alive()


class TestAsyncToSyncCancellation:
    @pytest.mark.req("ASYNC-14", "ASYNC-6", "SEAM-18")
    def test_keyboard_interrupt_cancels_then_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()
        cancelled = threading.Event()

        class _Hanging(AsyncHttpClient):
            async def execute(self, request: Request) -> AsyncResponse:
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                raise AssertionError("unreachable")  # pragma: no cover

        def _block_then_interrupt(self: _BridgedCall) -> None:
            assert started.wait(2)  # the async op is genuinely in flight
            raise KeyboardInterrupt

        monkeypatch.setattr(_BridgedCall, "_block", _block_then_interrupt)
        with AsyncToSyncHttpClient(_Hanging()) as bridge:
            with pytest.raises(KeyboardInterrupt):
                bridge.execute(_get())
            # The in-flight async operation was cancelled, not left dangling.
            assert cancelled.wait(2)

    @pytest.mark.req("ASYNC-5", "SEAM-30")
    def test_orphaned_response_closed_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _CloseTrackingAsyncBody()
        client = _RecordingAsyncClient(body=body)

        def _block_after_done(self: _BridgedCall) -> None:
            # The async op finished (a response exists) but the caller abandons.
            assert self._done.wait(2)
            raise KeyboardInterrupt

        monkeypatch.setattr(_BridgedCall, "_block", _block_after_done)
        with AsyncToSyncHttpClient(client) as bridge:
            with pytest.raises(KeyboardInterrupt):
                bridge.execute(_get())
            assert body.wait_closed(2)
        assert body.close_count == 1  # orphan closed exactly once — no leak, no double-close


class TestBridgedCallOwnership:
    """White-box ownership contract of the per-call handshake object."""

    @pytest.mark.req("ASYNC-20", "SEAM-16")
    def test_delivered_response_not_closed_on_late_abandon(self) -> None:
        body = _CloseTrackingAsyncBody()
        with AsyncToSyncHttpClient(_RecordingAsyncClient()) as bridge:
            loop = bridge._loop_bg.loop
            call = _BridgedCall(bridge._loop_bg)
            response = _async_response(_get(), body)
            # Mimic the loop-thread completion handshake, then take delivery.
            with call._lock:
                call._result = response
            call._done.set()
            assert call.wait() is response
            # A cancellation that arrives after delivery must NOT close a
            # response the caller already owns.
            call._abandon()
            # Flush the loop so any (erroneous) scheduled close would run.
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
            assert body.close_count == 0


class TestUnwrapException:
    @pytest.mark.req("ASYNC-13")
    def test_passes_through_sdk_error_unchanged(self) -> None:
        boom = ServiceRequestError("boom")
        assert _unwrap_exception(boom) is boom

    @pytest.mark.req("ASYNC-13", "ASYNC-14")
    def test_translates_concurrent_cancelled_to_asyncio(self) -> None:
        original = asyncio.CancelledError()
        wrapper = concurrent.futures.CancelledError()
        wrapper.__cause__ = original
        result = _unwrap_exception(wrapper)
        assert isinstance(result, asyncio.CancelledError)
        assert result is original

    def test_bare_concurrent_cancelled_becomes_asyncio(self) -> None:
        result = _unwrap_exception(concurrent.futures.CancelledError())
        assert isinstance(result, asyncio.CancelledError)

    @pytest.mark.req("ASYNC-13")
    def test_cycle_in_cause_chain_terminates(self) -> None:
        # A self-referential __cause__/__context__ cycle must not spin forever.
        wrapper = concurrent.futures.CancelledError()
        filler = RuntimeError("noise")
        wrapper.__cause__ = filler
        filler.__cause__ = wrapper  # cycle
        filler.__context__ = wrapper
        result = _unwrap_exception(wrapper)  # must return, not hang or recurse
        assert isinstance(result, asyncio.CancelledError)
