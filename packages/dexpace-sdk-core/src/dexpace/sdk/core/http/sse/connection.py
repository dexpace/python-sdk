# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Reconnecting Server-Sent Events client built on the sans-io parser.

``SseConnection`` / ``AsyncSseConnection`` drive a long-lived SSE stream
through the pipeline and transparently reconnect when it drops, replaying the
``Last-Event-ID`` header and honouring the server's ``retry:`` hint with
jittered backoff. Parsing is delegated to the existing sans-io parser; this
layer owns only the connection lifecycle.

Semantics follow the browser ``EventSource``: a reconnect is attempted on both
a transient transport error and a clean end-of-stream, while a non-success HTTP
status is a permanent failure (raised, never retried). The caller ends the
stream by breaking the loop (for example on a ``data: [DONE]`` sentinel).
"""

from __future__ import annotations

import random
import threading
from typing import TYPE_CHECKING, cast

from ...errors import HttpResponseError, ServiceRequestError, ServiceResponseError
from ...pipeline.dispatch import (
    AsyncPipelineLike,
    SendAsync,
    SendSync,
    SyncPipelineLike,
)
from ...util.clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock
from ..context.dispatch_context import DispatchContext
from ..response.async_response_body import _shielded_cleanup
from .parser import SseParser, parse_async_events

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator, Iterator
    from types import TracebackType
    from typing import Self

    from ..request.request import Request
    from ..response.async_response import AsyncResponse
    from ..response.response import Response
    from .parser import SseEvent

_LAST_EVENT_ID: str = "Last-Event-ID"
_DEFAULT_RETRY_SECONDS: float = 3.0
_DEFAULT_MAX_BACKOFF: float = 30.0
_DEFAULT_JITTER: float = 0.1

#: Transient failures that trigger a reconnect rather than propagating. A
#: clean end-of-stream reconnects too (handled separately); anything outside
#: this set — including ``CancelledError`` and ``HttpResponseError`` — is fatal.
_TRANSIENT: tuple[type[BaseException], ...] = (
    ServiceRequestError,
    ServiceResponseError,
    OSError,
)


def _resume_request(initial: Request, last_event_id: str | None) -> Request:
    """Return ``initial``, stamping ``Last-Event-ID`` when an id is known."""
    if last_event_id:
        return initial.with_header(_LAST_EVENT_ID, last_event_id)
    return initial


def _last_event_id_of(event: SseEvent, current: str | None) -> str | None:
    """Return the id to resume from after ``event``.

    An explicit empty id clears the stored id (per the SSE spec), so the next
    reconnect omits the ``Last-Event-ID`` header.
    """
    if event.id is not None:
        return event.id or None
    return current


def _next_backoff(
    retry_base: float, failures: int, max_backoff: float, jitter: float, rand: random.Random
) -> float:
    """Exponential backoff with an upward-only jitter, capped at ``max_backoff``.

    Jitter only lengthens the wait, so a fleet de-synchronises without any
    client reconnecting sooner than the computed delay.
    """
    bounded = min(max_backoff, retry_base * (2**failures))
    # cast required: random.Random.uniform is typed as returning Any in typeshed,
    # so mypy raises [no-any-return] without it.
    return cast(float, bounded * rand.uniform(1.0, 1.0 + jitter))


class SseConnection:
    """Synchronous reconnecting SSE stream.

    Iterate it (directly or via ``with``) to receive ``SseEvent``s across
    reconnections. Each (re)connection is dispatched through ``source`` — a
    ``Pipeline`` (run with a fresh dispatch context per connection) or a bare
    ``Request -> Response`` callable — so retry, auth, and tracing apply per
    connection.

    Args:
        source: A sync pipeline or a send-callable.
        initial_request: The request opening the stream; reused for every
            reconnection with an updated ``Last-Event-ID``.
        last_event_id: Seed id to resume a previously-interrupted stream.
        default_retry: Backoff base (seconds) used until the server sends a
            ``retry:`` value.
        max_backoff: Ceiling on any single reconnect delay.
        max_reconnects: Maximum consecutive failed reconnections before
            iteration raises. ``None`` reconnects indefinitely.
        jitter: Upward jitter fraction applied to the backoff.
        clock: Time source for backoff sleeps (injected for tests).
        rand: RNG for jitter (injected for tests).
        dispatch_factory: Builds the dispatch context per connection when
            ``source`` is a pipeline. Defaults to ``DispatchContext.noop``.
    """

    __slots__ = (
        "_clock",
        "_closed",
        "_dispatch_factory",
        "_initial",
        "_iterated",
        "_jitter",
        "_last_event_id",
        "_lock",
        "_max_backoff",
        "_max_reconnects",
        "_rand",
        "_response",
        "_retry_base",
        "_send",
    )

    def __init__(
        self,
        source: SyncPipelineLike | SendSync,
        initial_request: Request,
        *,
        last_event_id: str | None = None,
        default_retry: float = _DEFAULT_RETRY_SECONDS,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_reconnects: int | None = None,
        jitter: float = _DEFAULT_JITTER,
        clock: Clock = SYSTEM_CLOCK,
        rand: random.Random | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._initial = initial_request
        self._last_event_id = last_event_id
        self._retry_base = default_retry
        self._max_backoff = max_backoff
        self._max_reconnects = max_reconnects
        self._jitter = jitter
        self._clock = clock
        self._rand = rand if rand is not None else random.Random()
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._response: Response | None = None
        # The close-once flag is guarded by an explicit lock rather than relying
        # on the GIL, so cross-thread ``close`` stays race-free on free-threaded
        # CPython (PEP 703).
        self._lock = threading.Lock()
        self._closed = False
        self._iterated = False
        self._send = self._normalise(source)

    def _normalise(self, source: SyncPipelineLike | SendSync) -> SendSync:
        if isinstance(source, SyncPipelineLike):
            pipeline = source

            def send(request: Request) -> Response:
                return pipeline.run(request, self._dispatch_factory())

            return send
        return source

    def _release(self) -> None:
        """Release the currently-owned response exactly once.

        The read-and-clear of the response slot happens under ``_lock``, so two
        callers racing (for example ``close`` invoked from another thread while
        iteration parks in the transport) can never both observe the same
        response: only the caller that atomically takes the slot goes on to
        close it. The close itself runs outside the lock so a slow transport
        teardown never blocks a racing caller.
        """
        with self._lock:
            response = self._response
            self._response = None
        if response is not None:
            response.close()

    def _release_suppressing(self, primary: BaseException) -> None:
        """Release the response while ``primary`` is propagating.

        A failure raised by the release is demoted onto ``primary.__context__``
        instead of replacing ``primary``: the mid-stream failure the caller
        needs to see must never be masked by a secondary teardown error.
        """
        try:
            self._release()
        except BaseException as close_error:
            # Demoted, not swallowed: the primary exception re-raised by the
            # caller carries this teardown error on its ``__context__``.
            primary.__context__ = close_error

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        """Close the connection: release the response and stop reconnecting.

        Idempotent and safe to call from any thread, including while another
        thread iterates. A second call is a no-op rather than a double-release.
        """
        with self._lock:
            self._closed = True
        self._release()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[SseEvent]:
        """Return the single-pass event iterator.

        Raises:
            RuntimeError: If the connection is already closed or was iterated
                before. An SSE stream cannot be restarted by re-iterating the
                facade; open a fresh connection instead.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("SseConnection is closed")
            if self._iterated:
                raise RuntimeError("SseConnection supports a single iteration")
            self._iterated = True
        return self._run()

    def _run(self) -> Iterator[SseEvent]:
        failures = 0
        last_error: BaseException | None = None
        try:
            while True:
                response = self._connect(_resume_request(self._initial, self._last_event_id))
                progressed, error = yield from self._stream(response)
                if error is not None:
                    last_error = error
                self._release()
                if self._is_closed():
                    return
                if progressed:
                    failures = 0
                if self._max_reconnects is not None and failures >= self._max_reconnects:
                    raise ServiceResponseError("SSE reconnect budget exhausted") from last_error
                self._clock.sleep(
                    _next_backoff(
                        self._retry_base, failures, self._max_backoff, self._jitter, self._rand
                    )
                )
                failures += 1
        except BaseException as primary:
            # The loop only ever exits by raising — a mid-stream failure, an
            # exhausted reconnect budget, or ``GeneratorExit`` when the caller
            # stops early — so releasing here covers every termination path and
            # runs before the exception reaches the caller.
            self._release_suppressing(primary)
            raise

    def _connect(self, request: Request) -> Response:
        response = self._send(request)
        with self._lock:
            self._response = response
        if not response.status.is_success:
            self._release()
            raise HttpResponseError(response=response)
        return response

    def _stream(
        self, response: Response
    ) -> Generator[SseEvent, None, tuple[bool, BaseException | None]]:
        """Yield one connection's events; return (progressed, transient_error).

        A transient transport error mid-stream ends the generator normally (so
        the caller reconnects) and is returned so the caller can chain it as the
        cause if the reconnect budget is later exhausted; other exceptions
        propagate. A ``retry:`` hint that arrived without a data event is picked
        up from the parser's sticky state after iteration.
        """
        progressed = False
        error: BaseException | None = None
        body = response.body
        if body is None:
            return progressed, error
        parser = SseParser()
        try:
            for chunk in body.iter_bytes():
                parser.feed(chunk)
                for event in parser.drain():
                    self._last_event_id = _last_event_id_of(event, self._last_event_id)
                    progressed = True
                    yield event
            for event in parser.end():
                self._last_event_id = _last_event_id_of(event, self._last_event_id)
                progressed = True
                yield event
        except _TRANSIENT as exc:
            error = exc
        if parser.retry is not None:
            self._retry_base = parser.retry / 1000.0
        return progressed, error


class AsyncSseConnection:
    """Asynchronous twin of `SseConnection`.

    Mirrors the sync client exactly with ``async`` iteration semantics. Each
    (re)connection is dispatched through ``source`` — an ``AsyncPipeline`` (run
    with a fresh dispatch context per connection) or an async
    ``Request -> AsyncResponse`` callable. The response is closed through the
    shielded-cleanup convention, so a cancelled consumer still releases the
    transport handle before the cancellation continues to propagate.

    Args:
        source: An async pipeline or an async send-callable.
        initial_request: The request opening the stream; reused for every
            reconnection with an updated ``Last-Event-ID``.
        last_event_id: Seed id to resume a previously-interrupted stream.
        default_retry: Backoff base (seconds) used until the server sends a
            ``retry:`` value.
        max_backoff: Ceiling on any single reconnect delay.
        max_reconnects: Maximum consecutive failed reconnections before
            iteration raises. ``None`` reconnects indefinitely.
        jitter: Upward jitter fraction applied to the backoff.
        clock: Async time source for backoff sleeps (injected for tests).
        rand: RNG for jitter (injected for tests).
        dispatch_factory: Builds the dispatch context per connection when
            ``source`` is a pipeline. Defaults to ``DispatchContext.noop``.
    """

    __slots__ = (
        "_clock",
        "_closed",
        "_dispatch_factory",
        "_initial",
        "_iterated",
        "_jitter",
        "_last_event_id",
        "_lock",
        "_max_backoff",
        "_max_reconnects",
        "_rand",
        "_response",
        "_retry_base",
        "_send",
    )

    def __init__(
        self,
        source: AsyncPipelineLike | SendAsync,
        initial_request: Request,
        *,
        last_event_id: str | None = None,
        default_retry: float = _DEFAULT_RETRY_SECONDS,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_reconnects: int | None = None,
        jitter: float = _DEFAULT_JITTER,
        clock: AsyncClock = ASYNC_SYSTEM_CLOCK,
        rand: random.Random | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._initial = initial_request
        self._last_event_id = last_event_id
        self._retry_base = default_retry
        self._max_backoff = max_backoff
        self._max_reconnects = max_reconnects
        self._jitter = jitter
        self._clock = clock
        self._rand = rand if rand is not None else random.Random()
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._response: AsyncResponse | None = None
        # The close-once flag is guarded by an explicit lock, not the GIL, so
        # the slot read-and-clear stays race-free on free-threaded CPython even
        # when ``aclose`` is driven from a different thread's loop.
        self._lock = threading.Lock()
        self._closed = False
        self._iterated = False
        self._send = self._normalise(source)

    def _normalise(self, source: AsyncPipelineLike | SendAsync) -> SendAsync:
        if isinstance(source, AsyncPipelineLike):
            pipeline = source

            async def send(request: Request) -> AsyncResponse:
                return await pipeline.run(request, self._dispatch_factory())

            return send
        return source

    async def _arelease(self) -> None:
        """Release the currently-owned response exactly once, cancel-safely.

        The slot is taken under ``_lock`` so a racing releaser never closes the
        same response twice; the ``await`` runs outside the lock. The close is
        routed through ``_shielded_cleanup`` so a cancelled consumer still
        releases the transport handle before the cancellation continues.
        """
        with self._lock:
            response = self._response
            self._response = None
        if response is not None:
            await _shielded_cleanup(response.close())

    async def _arelease_suppressing(self, primary: BaseException) -> None:
        """Release the response while ``primary`` is propagating.

        A teardown failure is demoted onto ``primary.__context__`` rather than
        masking the primary exception the caller must see.
        """
        try:
            await self._arelease()
        except BaseException as close_error:
            # Demoted, not swallowed: the primary exception re-raised by the
            # caller carries this teardown error on its ``__context__``.
            primary.__context__ = close_error

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    async def aclose(self) -> None:
        """Close the connection: release the response and stop reconnecting.

        Idempotent and cancel-safe; a second call is a no-op rather than a
        double-release.
        """
        with self._lock:
            self._closed = True
        await self._arelease()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[SseEvent]:
        """Return the single-pass async event iterator.

        Raises:
            RuntimeError: If the connection is already closed or was iterated
                before. An SSE stream cannot be restarted by re-iterating the
                facade; open a fresh connection instead.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("AsyncSseConnection is closed")
            if self._iterated:
                raise RuntimeError("AsyncSseConnection supports a single iteration")
            self._iterated = True
        return self._events()

    async def _events(self) -> AsyncIterator[SseEvent]:
        failures = 0
        last_error: BaseException | None = None
        try:
            while True:
                response = await self._connect(_resume_request(self._initial, self._last_event_id))
                progressed = False
                body = response.body
                if body is not None:
                    stream = parse_async_events(body.aiter_bytes())
                    try:
                        async with stream:
                            async for event in stream:
                                self._last_event_id = _last_event_id_of(event, self._last_event_id)
                                progressed = True
                                yield event
                    except _TRANSIENT as exc:
                        last_error = exc
                    if stream.retry is not None:
                        self._retry_base = stream.retry / 1000.0
                await self._arelease()
                if self._is_closed():
                    return
                if progressed:
                    failures = 0
                if self._max_reconnects is not None and failures >= self._max_reconnects:
                    raise ServiceResponseError("SSE reconnect budget exhausted") from last_error
                await self._clock.sleep(
                    _next_backoff(
                        self._retry_base, failures, self._max_backoff, self._jitter, self._rand
                    )
                )
                failures += 1
        except BaseException as primary:
            # Every loop exit is an exception (mid-stream failure, exhausted
            # budget, or the cancellation/close thrown into the generator), so
            # releasing here releases before propagating on all of them.
            await self._arelease_suppressing(primary)
            raise

    async def _connect(self, request: Request) -> AsyncResponse:
        response = await self._send(request)
        with self._lock:
            self._response = response
        if not response.status.is_success:
            await self._arelease()
            raise HttpResponseError(response=response)
        return response


__all__ = ["AsyncSseConnection", "SseConnection"]
