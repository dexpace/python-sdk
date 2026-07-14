# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Adapt an ``AsyncHttpClient`` for use from a synchronous pipeline.

``AsyncToSyncHttpClient`` drives an async transport from blocking code. There
may be no event loop on the calling thread — and even if there were, running the
transport on it would reenter the caller's loop — so the bridge owns a dedicated
background thread with its own loop and blocks the caller for the result.

Three contracts make this safe rather than merely functional:

- Exceptions surface with their exact type. A caller's ``except SomeSdkError``
  still matches; ``_unwrap_exception`` peels the ``concurrent.futures``
  cancellation wrapper the thread boundary can introduce, cycle-safely.
- ``KeyboardInterrupt`` cancels the in-flight async operation, then re-raises —
  never swallowed, never left dangling.
- A response produced after the caller stops waiting is orphaned; the bridge
  closes it exactly once. A response already delivered belongs to the caller and
  is never closed out from under them.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from typing import TYPE_CHECKING, Any, Self

from ..http.response.response import Response
from ..http.response.response_body import ResponseBody
from ..instrumentation.correlation import capture_context
from .http_client import HttpClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator
    from contextvars import Context
    from types import TracebackType

    from ..http.common.media_type import MediaType
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from ..http.response.async_response_body import AsyncResponseBody
    from .async_http_client import AsyncHttpClient

_POLL_SECONDS = 0.05
_CHUNK_SENTINEL = object()


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Return the exception a sync caller should see for a bridged failure.

    Crossing the sync/async boundary can surface cancellation as a
    ``concurrent.futures.CancelledError`` — an ``Exception`` subclass that wraps
    the async-native ``asyncio.CancelledError`` (a ``BaseException``). Left
    as-is, an ``except Exception`` site would swallow a cancellation meant to
    propagate. This peels the wrapper back to the original cancellation. Every
    other exception — including every ``SdkError`` — is returned unchanged, so a
    caller's ``except SpecificError`` still matches the exact transport type.

    The wrapper's ``__cause__`` / ``__context__`` chain is walked to recover the
    original ``asyncio.CancelledError``. The walk records visited exception ids
    and stops on a repeat, so a self-referential (cyclic) chain terminates
    instead of spinning forever.

    Args:
        exc: The exception raised while retrieving the bridged result.

    Returns:
        The exception the caller should observe.
    """
    if not isinstance(exc, concurrent.futures.CancelledError):
        return exc
    seen = {id(exc)}
    current: BaseException | None = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen:
        if isinstance(current, asyncio.CancelledError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return asyncio.CancelledError()


async def _safe_close(response: AsyncResponse) -> None:
    """Close an orphaned response best-effort, swallowing cleanup failures.

    Runs on the background loop for a response the caller abandoned. A failure
    here has no caller to surface to and must not crash the loop, so it is
    deliberately swallowed — the response is being discarded regardless.
    """
    with contextlib.suppress(Exception):
        await response.close()


class _BridgedCall:
    """Per-call handshake between the calling thread and the background loop.

    The loop thread runs the transport coroutine and records its outcome; the
    calling thread blocks in `wait` for it. The class enforces response
    ownership: a result the caller takes delivery of is theirs to close, while a
    result produced after the caller abandons the call is closed here exactly
    once (guarded by ``_closed`` under ``_lock``).
    """

    __slots__ = (
        "_abandoned",
        "_bg",
        "_closed",
        "_delivered",
        "_done",
        "_exc",
        "_lock",
        "_result",
        "_task",
    )

    def __init__(self, bg: _BackgroundLoop) -> None:
        self._bg = bg
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._result: AsyncResponse | None = None
        self._exc: BaseException | None = None
        self._task: asyncio.Task[AsyncResponse] | None = None
        self._abandoned = False
        self._closed = False
        self._delivered = False

    def start(self, coro: Coroutine[Any, Any, AsyncResponse], context: Context) -> None:
        """Create the transport task inside ``context``. Runs on the loop thread."""
        self._task = self._bg.spawn(coro, context)
        self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[AsyncResponse]) -> None:
        """Record the task outcome and wake the caller. Runs on the loop thread."""
        if task.cancelled():
            self._exc = asyncio.CancelledError()
        elif (exc := task.exception()) is not None:
            self._exc = exc
        else:
            with self._lock:
                self._result = task.result()
                self._close_if_orphaned_locked()
        self._done.set()

    def wait(self) -> AsyncResponse:
        """Block for the outcome, then deliver it or raise. Runs on the caller.

        Returns:
            The response, whose ownership transfers to the caller.

        Raises:
            KeyboardInterrupt: Re-raised after the in-flight operation is
                cancelled.
            BaseException: The transport's failure, unwrapped to its original
                type.
        """
        try:
            self._block()
        except KeyboardInterrupt:
            self._abandon()
            raise
        if self._exc is not None:
            raise _unwrap_exception(self._exc)
        with self._lock:
            self._delivered = True
            assert self._result is not None
            return self._result

    def _block(self) -> None:
        """Wait for completion, polling so a signal stays deliverable."""
        while not self._done.wait(_POLL_SECONDS):
            pass

    def _abandon(self) -> None:
        """Cancel the in-flight task and arrange orphan cleanup. On the caller."""
        with self._lock:
            self._abandoned = True
            self._close_if_orphaned_locked()
        self._bg.loop.call_soon_threadsafe(self._cancel_task)

    def _close_if_orphaned_locked(self) -> None:
        """Schedule an orphan close if warranted. Caller must hold ``_lock``."""
        if self._delivered or self._closed or not self._abandoned:
            return
        if self._result is not None:
            self._closed = True
            self._bg.loop.call_soon_threadsafe(self._spawn_close, self._result)

    def _cancel_task(self) -> None:
        """Cancel the transport task. Runs on the loop thread."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _spawn_close(self, response: AsyncResponse) -> None:
        """Fire off the (tracked) orphan close coroutine. Runs on the loop thread."""
        self._bg.spawn(_safe_close(response))


class _BackgroundLoop:
    """A dedicated daemon thread running its own asyncio event loop.

    Every task it runs is tracked in ``_tasks`` (touched only on the loop
    thread, so no lock is needed) and given a done-callback that drops it. That
    keeps a strong reference to each in-flight task — asyncio holds only a weak
    one — so a fire-and-forget orphan close cannot be garbage-collected
    mid-flight, and `close` can drain everything before stopping the loop.
    """

    __slots__ = ("_loop", "_tasks", "_thread")

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._tasks: set[asyncio.Task[Any]] = set()
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(ready,),
            name="dexpace-sdk-async-bridge",
            daemon=True,
        )
        self._thread.start()
        ready.wait()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def _run(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(ready.set)
        try:
            self._loop.run_forever()
        finally:
            asyncio.set_event_loop(None)
            self._loop.close()

    def spawn[T](
        self, coro: Coroutine[Any, Any, T], context: Context | None = None
    ) -> asyncio.Task[T]:
        """Create a tracked task on the loop. Must run on the loop thread."""
        task = self._loop.create_task(coro, context=context)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def submit(
        self,
        coro: Coroutine[Any, Any, AsyncResponse],
        context: Context,
        call: _BridgedCall,
    ) -> None:
        """Schedule ``call.start`` on the loop from the calling thread."""
        self._loop.call_soon_threadsafe(call.start, coro, context)

    def close(self) -> None:
        """Drain in-flight tasks, then stop the loop and join the thread."""
        done = threading.Event()
        self._loop.call_soon_threadsafe(self._drain_then_stop, done)
        done.wait()
        self._thread.join()

    def _drain_then_stop(self, done: threading.Event) -> None:
        """Await outstanding tasks, then stop the loop. Runs on the loop thread.

        Re-checks after each drain: a drained task (a cancelled transport call)
        can itself spawn a late orphan close, so the loop stops only once no
        tracked task remains pending.
        """
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            gather = asyncio.gather(*pending, return_exceptions=True)
            gather.add_done_callback(lambda _: self._drain_then_stop(done))
            return
        self._loop.stop()
        done.set()


class AsyncToSyncHttpClient(HttpClient):
    """Drive an ``AsyncHttpClient`` from sync code on a background loop thread.

    Owns a dedicated event-loop thread for its lifetime; use it as a context
    manager (or call `close`) to shut that thread down. The caller's contextvars
    are captured at submission so the transport observes the correct correlation
    context, not whatever the loop thread happened to hold.
    """

    __slots__ = ("_client", "_loop_bg")

    def __init__(self, client: AsyncHttpClient) -> None:
        """Wrap ``client``, spinning up the background event-loop thread."""
        self._client = client
        self._loop_bg = _BackgroundLoop()

    def execute(self, request: Request) -> Response:
        """Run the async transport to completion and adapt its response.

        Args:
            request: The request to send, threaded through unchanged.

        Returns:
            A ``Response`` mirroring the ``AsyncResponse``, with its body adapted
            to read through the background loop.
        """
        context = capture_context()
        call = _BridgedCall(self._loop_bg)
        self._loop_bg.submit(self._client.execute(request), context, call)
        async_response = call.wait()
        return _to_sync_response(async_response, self._loop_bg.loop)

    def close(self) -> None:
        """Shut down the background event-loop thread."""
        self._loop_bg.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _to_sync_response(response: AsyncResponse, loop: asyncio.AbstractEventLoop) -> Response:
    """Adapt an ``AsyncResponse`` to a sync ``Response`` reading via ``loop``."""
    body = response.body
    sync_body = None if body is None else _LoopResponseBody(body, loop)
    return Response(
        request=response.request,
        protocol=response.protocol,
        status=response.status,
        headers=response.headers,
        reason=response.reason,
        body=sync_body,
    )


async def _anext(aiter: AsyncIterator[bytes]) -> object:
    """Advance an async iterator once, or return the sentinel at exhaustion."""
    try:
        return await aiter.__anext__()
    except StopAsyncIteration:
        return _CHUNK_SENTINEL


class _LoopResponseBody(ResponseBody):
    """Adapt an ``AsyncResponseBody`` to sync by driving reads on the loop.

    Every read and the final close run on the background loop and block the
    calling thread. Single-use semantics come from the wrapped body: its
    ``aiter_bytes`` claims the once-guard, so a second ``iter_bytes`` raises.
    """

    __slots__ = ("_closed", "_inner", "_loop")

    def __init__(self, inner: AsyncResponseBody, loop: asyncio.AbstractEventLoop) -> None:
        self._inner = inner
        self._loop = loop
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        return self._inner.content_length()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        aiter = self._inner.aiter_bytes(chunk_size)
        return self._iter(aiter)

    def _iter(self, aiter: AsyncIterator[bytes]) -> Iterator[bytes]:
        try:
            while True:
                chunk = self._run(_anext(aiter))
                if chunk is _CHUNK_SENTINEL:
                    return
                assert isinstance(chunk, bytes)
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._run(self._inner.close())

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


__all__ = ["AsyncToSyncHttpClient"]
