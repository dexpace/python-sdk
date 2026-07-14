# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Recovery-fold battery: outcome sum type, conversion / close / non-collapse.

Pins the internal recovery machinery that folds request/response-step
exceptions into a ``Success | Failure`` outcome:

- **Conversion.** Every ``Exception`` from a producer / response-side step
  converts to a ``Failure``; a recovery step runs on every outcome; a step
  that throws while handling an outcome converts-and-continues.
- **Non-collapse (the sharp edge).** ``asyncio.CancelledError`` and
  ``KeyboardInterrupt`` are ``BaseException``s, not ``Exception``s, and must
  never fold into a ``Failure``; they propagate unconverted, with any held
  response closed first.
- **Identity.** The final unwrap re-raises the exact original exception
  instance, not a copy or wrapper.
- **Close-during-error-handling.** A close failure while unwinding a held
  response is chained onto the original error, never lost or promoted.
- **Status mapping.** Only 400-599 is an error status; the error body is
  buffered up to a shared cap inside a scope that still guarantees close.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator

import pytest

from dexpace.sdk.core.errors import HttpResponseError, ResourceNotFoundError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, ResponseBody, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.loggable_response_body import LoggableResponseBody
from dexpace.sdk.core.pipeline import PipelineContext
from dexpace.sdk.core.pipeline._async_recovery import (
    afrom_status,
    amap_success,
    aproduce,
    arecover,
)
from dexpace.sdk.core.pipeline._async_sansio_runner import _AsyncSansIOResponseRunner
from dexpace.sdk.core.pipeline._outcome import Failure, Success, unwrap
from dexpace.sdk.core.pipeline._recovery import (
    from_status,
    map_success,
    produce,
    recover,
)
from dexpace.sdk.core.pipeline._sansio_runner import _SansIOResponseRunner

# --------------------------------------------------------------------------- #
# Close-tracking / failing response bodies                                     #
# --------------------------------------------------------------------------- #


class _Body(ResponseBody):
    """Sync response body: counts closes; can fail on ``close`` / ``iter_bytes``."""

    __slots__ = ("_data", "_fail_close", "_fail_read", "close_count")

    def __init__(
        self,
        data: bytes = b"",
        *,
        fail_close: bool = False,
        fail_read: bool = False,
    ) -> None:
        self._data = data
        self._fail_close = fail_close
        self._fail_read = fail_read
        self.close_count = 0

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._fail_read:
            raise OSError("read blew up")
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start : start + chunk_size]

    def close(self) -> None:
        self.close_count += 1
        if self._fail_close:
            raise RuntimeError("close blew up")

    @property
    def closed(self) -> bool:
        return self.close_count > 0


class _AsyncBody(AsyncResponseBody):
    """Async twin of ``_Body``."""

    __slots__ = ("_data", "_fail_close", "_fail_read", "close_count")

    def __init__(
        self,
        data: bytes = b"",
        *,
        fail_close: bool = False,
        fail_read: bool = False,
    ) -> None:
        self._data = data
        self._fail_close = fail_close
        self._fail_read = fail_read
        self.close_count = 0

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if self._fail_read:
            raise OSError("read blew up")
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start : start + chunk_size]

    async def close(self) -> None:
        self.close_count += 1
        if self._fail_close:
            raise RuntimeError("close blew up")

    @property
    def closed(self) -> bool:
        return self.close_count > 0


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/x"))


def _response(status: Status = Status.OK, body: ResponseBody | None = None) -> Response:
    return Response(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)


def _async_response(
    status: Status = Status.OK, body: AsyncResponseBody | None = None
) -> AsyncResponse:
    return AsyncResponse(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)


# --------------------------------------------------------------------------- #
# produce: Exception -> Failure; BaseException propagates unconverted          #
# --------------------------------------------------------------------------- #


class TestProduce:
    def test_success_wraps_response(self) -> None:
        response = _response()
        outcome = produce(lambda: response)
        assert isinstance(outcome, Success)
        assert outcome.value is response

    def test_exception_folds_to_failure_preserving_identity(self) -> None:
        boom = ValueError("boom")

        def source() -> Response:
            raise boom

        outcome = produce(source)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom

    def test_keyboard_interrupt_propagates_unconverted(self) -> None:
        def source() -> Response:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            produce(source)

    def test_cancelled_error_propagates_unconverted(self) -> None:
        def source() -> Response:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            produce(source)


# --------------------------------------------------------------------------- #
# map_success: runs only on Success; closes held response on failure           #
# --------------------------------------------------------------------------- #


class TestMapSuccess:
    def test_runs_on_success(self) -> None:
        stamped = _response(Status.ACCEPTED)
        outcome = map_success(Success(_response()), lambda _r: stamped)
        assert isinstance(outcome, Success)
        assert outcome.value is stamped

    def test_skips_a_failure(self) -> None:
        failure: Failure = Failure(ValueError("x"))
        called = False

        def step(_r: Response) -> Response:
            nonlocal called
            called = True
            return _r

        assert map_success(failure, step) is failure
        assert called is False

    def test_exception_closes_held_response_and_folds(self) -> None:
        body = _Body()
        held = _response(body=body)
        boom = ValueError("step failed")

        def step(_r: Response) -> Response:
            raise boom

        outcome = map_success(Success(held), step)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom
        assert body.close_count == 1  # held response closed before folding

    def test_keyboard_interrupt_closes_held_then_propagates(self) -> None:
        body = _Body()
        held = _response(body=body)

        def step(_r: Response) -> Response:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            map_success(Success(held), step)
        assert body.close_count == 1  # held response closed FIRST

    def test_close_failure_during_error_handling_is_chained(self) -> None:
        body = _Body(fail_close=True)
        held = _response(body=body)
        boom = ValueError("step failed")

        def step(_r: Response) -> Response:
            raise boom

        outcome = map_success(Success(held), step)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom  # original wins, not the close error
        notes = getattr(outcome.error, "__notes__", [])
        assert any("close" in note for note in notes)  # close error chained on


# --------------------------------------------------------------------------- #
# recover: runs on every outcome                                               #
# --------------------------------------------------------------------------- #


class TestRecover:
    def test_runs_on_both_outcomes(self) -> None:
        seen: list[object] = []

        def cleanup(outcome: Success[Response] | Failure) -> Success[Response] | Failure:
            seen.append(outcome)
            return outcome

        success = Success(_response())
        failure = Failure(ValueError("x"))
        assert recover(success, cleanup) is success
        assert recover(failure, cleanup) is failure
        assert seen == [success, failure]

    def test_exception_on_success_closes_held_and_folds(self) -> None:
        body = _Body()
        held = Success(_response(body=body))
        boom = RuntimeError("cleanup failed")

        def cleanup(_o: Success[Response] | Failure) -> Success[Response] | Failure:
            raise boom

        outcome = recover(held, cleanup)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom
        assert body.close_count == 1


# --------------------------------------------------------------------------- #
# unwrap: identity-preserving re-raise                                          #
# --------------------------------------------------------------------------- #


class TestUnwrap:
    def test_success_returns_response(self) -> None:
        response = _response()
        assert unwrap(Success(response)) is response

    def test_failure_reraises_original_instance(self) -> None:
        original = ValueError("the one true error")
        outcome = produce(_raise(original))
        try:
            unwrap(outcome)
        except ValueError as caught:
            assert caught is original  # exact object, not a copy/wrapper
        else:  # pragma: no cover - unwrap must raise
            pytest.fail("unwrap did not re-raise the failure")


def _raise(exc: BaseException) -> Callable[[], Response]:
    def _source() -> Response:
        raise exc

    return _source


async def _async_error_body(error: HttpResponseError) -> bytes:
    """Read the buffered body off an async error response for assertions."""
    response = error.response
    assert isinstance(response, AsyncResponse)
    assert response.body is not None
    return await response.body.bytes()


# --------------------------------------------------------------------------- #
# from_status: 400-599 error band, buffered body, guaranteed close             #
# --------------------------------------------------------------------------- #


class TestFromStatus:
    def test_success_status_passes_through_open(self) -> None:
        body = _Body(b"ok")
        response = _response(Status.OK, body)
        outcome = from_status(response)
        assert isinstance(outcome, Success)
        assert outcome.value is response
        assert body.close_count == 0  # success is handed back open

    def test_redirect_status_is_not_an_error(self) -> None:
        response = _response(Status.MOVED_PERMANENTLY, _Body())
        assert isinstance(from_status(response), Success)

    def test_client_error_folds_and_closes(self) -> None:
        body = _Body(b'{"detail": "nope"}')
        response = _response(Status.NOT_FOUND, body)
        outcome = from_status(response)
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, HttpResponseError)
        assert outcome.error.status is Status.NOT_FOUND
        assert body.close_count == 1  # error response closed

    def test_server_error_folds(self) -> None:
        response = _response(Status.INTERNAL_SERVER_ERROR, _Body(b"kaboom"))
        outcome = from_status(response)
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, HttpResponseError)

    def test_error_body_is_buffered_for_postmortem(self) -> None:
        response = _response(Status.BAD_REQUEST, _Body(b"the error detail"))
        outcome = from_status(response)
        assert isinstance(outcome, Failure)
        error = outcome.error
        assert isinstance(error, HttpResponseError)
        buffered = error.response
        assert buffered is not None
        assert isinstance(buffered.body, LoggableResponseBody)
        assert error.body_snapshot() == b"the error detail"

    def test_error_body_is_capped(self) -> None:
        response = _response(Status.BAD_REQUEST, _Body(b"x" * 50))
        outcome = from_status(response, max_body_bytes=8)
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, HttpResponseError)
        assert outcome.error.body_snapshot() == b"x" * 8

    def test_error_map_selects_subclass(self) -> None:
        response = _response(Status.NOT_FOUND, _Body(b""))
        outcome = from_status(response, error_map={404: ResourceNotFoundError})
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, ResourceNotFoundError)

    def test_close_guaranteed_even_if_buffering_fails(self) -> None:
        body = _Body(fail_read=True)
        response = _response(Status.SERVICE_UNAVAILABLE, body)
        outcome = from_status(response)
        assert isinstance(outcome, Failure)  # still produces the HTTP error
        assert isinstance(outcome.error, HttpResponseError)
        assert body.close_count == 1  # ... and the response was still closed
        notes = getattr(outcome.error, "__notes__", [])
        assert any("buffer" in note.lower() for note in notes)  # read failure noted


# --------------------------------------------------------------------------- #
# Async twin: CancelledError immunity + close-first                            #
# --------------------------------------------------------------------------- #


class TestAsyncRecovery:
    async def test_aproduce_success(self) -> None:
        response = _async_response()

        async def source() -> AsyncResponse:
            return response

        outcome = await aproduce(source)
        assert isinstance(outcome, Success)
        assert outcome.value is response

    async def test_aproduce_exception_folds(self) -> None:
        boom = ValueError("boom")

        async def source() -> AsyncResponse:
            raise boom

        outcome = await aproduce(source)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom

    async def test_amap_cancelled_closes_held_then_propagates(self) -> None:
        body = _AsyncBody()
        held = _async_response(body=body)

        async def step(_r: AsyncResponse) -> AsyncResponse:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await amap_success(Success(held), step)
        assert body.close_count == 1  # held response closed FIRST, never folded

    async def test_amap_exception_closes_and_folds(self) -> None:
        body = _AsyncBody()
        held = _async_response(body=body)
        boom = ValueError("async step failed")

        async def step(_r: AsyncResponse) -> AsyncResponse:
            raise boom

        outcome = await amap_success(Success(held), step)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom
        assert body.close_count == 1

    async def test_amap_close_failure_chained(self) -> None:
        body = _AsyncBody(fail_close=True)
        held = _async_response(body=body)
        boom = ValueError("async step failed")

        async def step(_r: AsyncResponse) -> AsyncResponse:
            raise boom

        outcome = await amap_success(Success(held), step)
        assert isinstance(outcome, Failure)
        assert outcome.error is boom
        notes = getattr(outcome.error, "__notes__", [])
        assert any("close" in note for note in notes)

    async def test_arecover_runs_on_both(self) -> None:
        seen: list[object] = []

        async def cleanup(
            outcome: Success[AsyncResponse] | Failure,
        ) -> Success[AsyncResponse] | Failure:
            seen.append(outcome)
            return outcome

        success = Success(_async_response())
        failure = Failure(ValueError("x"))
        assert await arecover(success, cleanup) is success
        assert await arecover(failure, cleanup) is failure
        assert seen == [success, failure]

    async def test_afrom_status_error_buffers_and_closes(self) -> None:
        body = _AsyncBody(b"async detail")
        response = _async_response(Status.NOT_FOUND, body)
        outcome = await afrom_status(response)
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, HttpResponseError)
        assert body.close_count == 1
        # No async LoggableResponseBody exists, so the buffered prefix is exposed
        # on the re-bodied response rather than through the sync-only body_snapshot.
        assert await _async_error_body(outcome.error) == b"async detail"

    async def test_afrom_status_error_body_is_capped(self) -> None:
        response = _async_response(Status.BAD_REQUEST, _AsyncBody(b"y" * 40))
        outcome = await afrom_status(response, max_body_bytes=6)
        assert isinstance(outcome, Failure)
        assert isinstance(outcome.error, HttpResponseError)
        assert await _async_error_body(outcome.error) == b"y" * 6

    async def test_afrom_status_close_guaranteed_when_buffering_fails(self) -> None:
        body = _AsyncBody(fail_read=True)
        response = _async_response(Status.INTERNAL_SERVER_ERROR, body)
        outcome = await afrom_status(response)
        assert isinstance(outcome, Failure)
        assert body.close_count == 1


# --------------------------------------------------------------------------- #
# Hardened SansIO response runners: original error wins over a close failure    #
# --------------------------------------------------------------------------- #


class _Chain(_SansIOResponseRunner):
    """Wire a synthetic ``next`` onto a response runner for direct testing."""


class TestResponseRunnerCloseChaining:
    def test_sync_step_error_wins_over_close_failure(self) -> None:
        body = _Body(fail_close=True)
        response = _response(body=body)
        boom = ValueError("response step failed")

        def step(_r: Response, _ctx: object) -> Response:
            raise boom

        runner = _SansIOResponseRunner(step)  # type: ignore[arg-type]
        runner.next = _StubNext(response)  # type: ignore[assignment]
        with pytest.raises(ValueError) as excinfo:
            runner.send(_request(), _ctx())
        assert excinfo.value is boom  # original propagates, not the close error
        assert body.close_count == 1
        assert any("close" in note for note in getattr(boom, "__notes__", []))

    def test_sync_cancelled_propagates_and_closes(self) -> None:
        body = _Body()
        response = _response(body=body)

        def step(_r: Response, _ctx: object) -> Response:
            raise asyncio.CancelledError

        runner = _SansIOResponseRunner(step)  # type: ignore[arg-type]
        runner.next = _StubNext(response)  # type: ignore[assignment]
        with pytest.raises(asyncio.CancelledError):
            runner.send(_request(), _ctx())
        assert body.close_count == 1

    async def test_async_step_error_wins_over_close_failure(self) -> None:
        body = _AsyncBody(fail_close=True)
        response = _async_response(body=body)
        boom = ValueError("async response step failed")

        async def step(_r: AsyncResponse, _ctx: object) -> AsyncResponse:
            raise boom

        runner = _AsyncSansIOResponseRunner(step)
        runner.next = _AsyncStubNext(response)  # type: ignore[assignment]
        with pytest.raises(ValueError) as excinfo:
            await runner.send(_request(), _ctx())
        assert excinfo.value is boom
        assert body.close_count == 1
        assert any("close" in note for note in getattr(boom, "__notes__", []))


class _StubNext:
    """A minimal ``next`` node returning a canned response."""

    def __init__(self, response: Response) -> None:
        self._response = response

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        return self._response


class _AsyncStubNext:
    def __init__(self, response: AsyncResponse) -> None:
        self._response = response

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return self._response


def _ctx() -> PipelineContext:
    from dexpace.sdk.core.http.context import DispatchContext
    from dexpace.sdk.core.instrumentation import (
        InstrumentationContext,
        SpanId,
        TraceFlags,
        TraceId,
        TraceIdType,
        TraceState,
    )
    from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN

    instr = InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId("0" * 32),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )
    dispatch = DispatchContext(instr)
    return PipelineContext(call=dispatch.to_request_context(_request()))
