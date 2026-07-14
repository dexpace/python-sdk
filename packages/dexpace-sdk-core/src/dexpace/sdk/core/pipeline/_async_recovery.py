# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Asynchronous recovery fold — the async twin of ``_recovery``.

Mirrors the synchronous fold's conversion, close, and non-collapse rules for
``AsyncResponse`` outcomes. The sharp edge is the same and matters more here:
``asyncio.CancelledError`` is a ``BaseException`` raised into a coroutine when a
task is cancelled, so an ``except Exception`` fold is structurally immune and a
cancellation is never converted into a ``Failure`` — it propagates unconverted,
with any held response closed first (the async ``close`` shields itself so the
transport handle is released even while the cancellation unwinds).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Final

from ..errors import HttpResponseError
from ..http.response.async_response_body import AsyncResponseBody
from ._outcome import Failure, Outcome, Success

if TYPE_CHECKING:
    from ..http.response.async_response import AsyncResponse

#: Cap, in bytes, on the buffered error-body preview. Mirrors the synchronous
#: fold's cap so both drivers bound error-body memory identically.
_ERROR_BODY_CAP: Final[int] = 1 << 20  # 1 MiB


async def aproduce(source: Callable[[], Awaitable[AsyncResponse]]) -> Outcome[AsyncResponse]:
    """Await a response-producing coroutine, folding an ``Exception`` into a ``Failure``.

    A ``BaseException`` (``asyncio.CancelledError``, ``KeyboardInterrupt``)
    propagates unconverted.

    Args:
        source: Zero-arg callable returning the response coroutine.

    Returns:
        A ``Success`` wrapping the response, or a ``Failure`` wrapping a raised
        ``Exception``.
    """

    async def thunk() -> Outcome[AsyncResponse]:
        return Success(await source())

    return await _afold(thunk, held=None)


async def amap_success(
    outcome: Outcome[AsyncResponse],
    step: Callable[[AsyncResponse], Awaitable[AsyncResponse]],
) -> Outcome[AsyncResponse]:
    """Apply a response-side coroutine to a ``Success``; pass a ``Failure`` through.

    Runs only on a ``Success``. If ``step`` raises an ``Exception`` the held
    response is closed before folding into a ``Failure``; a ``BaseException``
    still closes the held response, then propagates unconverted.

    Args:
        outcome: The current outcome.
        step: Response-side coroutine transform.

    Returns:
        The transformed ``Success``, the folded ``Failure``, or the untouched
        input ``Failure``.
    """
    if isinstance(outcome, Failure):
        return outcome
    response = outcome.value

    async def thunk() -> Outcome[AsyncResponse]:
        return Success(await step(response))

    return await _afold(thunk, held=response)


async def arecover(
    outcome: Outcome[AsyncResponse],
    cleanup: Callable[[Outcome[AsyncResponse]], Awaitable[Outcome[AsyncResponse]]],
) -> Outcome[AsyncResponse]:
    """Run a recovery/cleanup coroutine against any outcome.

    Runs whether the outcome is a ``Success`` or a ``Failure``. An ``Exception``
    folds into a ``Failure`` (closing a held ``Success`` response first); a
    ``BaseException`` propagates unconverted.

    Args:
        outcome: The current outcome.
        cleanup: Coroutine run against the outcome; may transform it.

    Returns:
        The cleanup's outcome, or a folded ``Failure``.
    """
    held = outcome.value if isinstance(outcome, Success) else None

    async def thunk() -> Outcome[AsyncResponse]:
        return await cleanup(outcome)

    return await _afold(thunk, held=held)


async def afrom_status(
    response: AsyncResponse,
    *,
    error_map: Mapping[int, type[HttpResponseError]] | None = None,
    max_body_bytes: int = _ERROR_BODY_CAP,
) -> Outcome[AsyncResponse]:
    """Map an async response's status band to an outcome.

    Only HTTP 400-599 is an error status; such a response folds into a
    ``Failure`` carrying an ``HttpResponseError`` whose body is buffered up to
    ``max_body_bytes`` inside a scope that still guarantees the response closes.
    Any other status yields a ``Success`` carrying the still-open response.

    Args:
        response: The response to classify.
        error_map: Optional status-code to exception-subclass mapping.
        max_body_bytes: Cap on the buffered error-body preview.

    Returns:
        A ``Success`` for a non-error status, else a ``Failure``.
    """
    if not (400 <= int(response.status) < 600):
        return Success(response)
    return Failure(await _ato_http_error(response, error_map, max_body_bytes))


async def _afold(
    thunk: Callable[[], Awaitable[Outcome[AsyncResponse]]],
    held: AsyncResponse | None,
) -> Outcome[AsyncResponse]:
    """Await ``thunk``, folding an ``Exception`` into a ``Failure``.

    A ``BaseException`` propagates unconverted. On any failure a ``held`` open
    response is closed first, with a close failure chained onto the exception.
    """
    try:
        return await thunk()
    except Exception as exc:
        if held is not None:
            await _aclose_and_chain(held, exc)
        return Failure(exc)
    except BaseException as exc:
        if held is not None:
            await _aclose_and_chain(held, exc)
        raise


async def _aclose_and_chain(response: AsyncResponse, primary: BaseException) -> None:
    """Close ``response``, chaining any close failure onto ``primary`` as a note."""
    try:
        await response.close()
    except Exception as close_error:  # a close failure must not mask the primary
        primary.add_note(f"response close during error handling failed: {close_error!r}")


async def _ato_http_error(
    response: AsyncResponse,
    error_map: Mapping[int, type[HttpResponseError]] | None,
    max_body_bytes: int,
) -> HttpResponseError:
    """Build the ``HttpResponseError`` for an error-status async response."""
    buffered, read_error = await _abuffer_error_response(response, max_body_bytes)
    error: HttpResponseError = HttpResponseError(response=buffered)
    if error_map:
        mapped = error_map.get(int(buffered.status))
        if mapped is not None:
            error = mapped(response=buffered)
    if read_error is not None:
        error.add_note(f"error response body buffering failed: {read_error!r}")
    return error


async def _abuffer_error_response(
    response: AsyncResponse,
    max_body_bytes: int,
) -> tuple[AsyncResponse, Exception | None]:
    """Buffer the async error body up to the cap, guaranteeing the response closes.

    Returns the response re-bodied with the captured prefix as an in-memory
    ``AsyncResponseBody`` (readable after the transport handle is released),
    plus any ``Exception`` raised while reading. There is no async
    ``LoggableResponseBody``, so the captured prefix is exposed on the response
    body itself rather than through ``HttpResponseError.body_snapshot``, which
    is sync-only.
    """
    body = response.body
    if body is None:
        return response, None
    try:
        prefix, read_error = await _aread_capped(body, max_body_bytes)
    finally:
        await response.close()
    return response.with_body(AsyncResponseBody.from_bytes(prefix)), read_error


async def _aread_capped(body: AsyncResponseBody, cap: int) -> tuple[bytes, Exception | None]:
    """Read up to ``cap`` bytes from ``body``, retaining a partial read on failure."""
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in body.aiter_bytes():
            if total >= cap:
                break
            take = chunk[: cap - total]
            chunks.append(take)
            total += len(take)
    except Exception as exc:  # partial capture is retained for post-mortem
        return b"".join(chunks), exc
    return b"".join(chunks), None


__all__ = ["afrom_status", "amap_success", "aproduce", "arecover"]
