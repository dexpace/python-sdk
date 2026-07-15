# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous recovery fold — convert step exceptions into a ``Success | Failure``.

The fold applies the spec's conversion, close, and non-collapse rules:

- **Conversion.** ``produce`` runs a response-producing callable (a request-side
  step or the transport) and folds any ``Exception`` into a ``Failure``.
  ``map_success`` runs a response-side step only when the current outcome is a
  ``Success``; ``recover`` runs a cleanup step against every outcome.
- **Non-collapse.** A ``BaseException`` (``asyncio.CancelledError``,
  ``KeyboardInterrupt``, ``SystemExit``) is a cancellation-class signal, not a
  fault, and is never folded into a ``Failure`` — it propagates unconverted.
- **Close-on-error.** When a step throws while a ``Success`` (an open response)
  is held, that response is closed before the exception folds or propagates. A
  failure from the close itself is chained onto the original error with a note
  rather than replacing it.

Ordering (which steps run, and in what order) and retry/redirect forks are the
pipeline's and the retry policy's job; this module only owns the folding
semantics for a single settled outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from ..errors import HttpResponseError
from ..http.response.loggable_response_body import LoggableResponseBody
from ..http.response.response_body import ResponseBody
from ._outcome import Failure, Outcome, Success

if TYPE_CHECKING:
    from ..http.response.response import Response

#: Cap, in bytes, on how much of an error response body is buffered for
#: post-mortem inspection. 1 MiB is generous for a structured error payload
#: while bounding the memory a hostile or runaway error body can pin.
_ERROR_BODY_CAP: Final[int] = 1 << 20  # 1 MiB


def produce(source: Callable[[], Response]) -> Outcome[Response]:
    """Run a response-producing callable, folding an ``Exception`` into a ``Failure``.

    Use for a request-side step or the transport itself: anything that turns the
    in-flight request into a response. A ``BaseException`` propagates unconverted.

    Args:
        source: Zero-arg callable returning the produced response.

    Returns:
        A ``Success`` wrapping the response, or a ``Failure`` wrapping a raised
        ``Exception``.
    """
    return _fold(lambda: Success(source()), held=None)


def map_success(
    outcome: Outcome[Response],
    step: Callable[[Response], Response],
) -> Outcome[Response]:
    """Apply a response-side step to a ``Success``; pass a ``Failure`` through.

    Response-side steps run only on a ``Success`` outcome. If the step raises an
    ``Exception``, the held response is closed before the exception folds into a
    new ``Failure``; if it raises a ``BaseException`` the held response is still
    closed, but the exception propagates unconverted.

    Args:
        outcome: The current outcome.
        step: Response-side transform applied to the held response.

    Returns:
        The transformed ``Success``, the folded ``Failure``, or the untouched
        input ``Failure``.
    """
    if isinstance(outcome, Failure):
        return outcome
    response = outcome.value
    return _fold(lambda: Success(step(response)), held=response)


def recover(
    outcome: Outcome[Response],
    cleanup: Callable[[Outcome[Response]], Outcome[Response]],
) -> Outcome[Response]:
    """Run a recovery/cleanup step against any outcome.

    Unlike a response-side step, ``cleanup`` runs whether the outcome is a
    ``Success`` or a ``Failure``. If it raises an ``Exception`` the result folds
    into a new ``Failure`` (closing a held ``Success`` response first); a
    ``BaseException`` propagates unconverted.

    Args:
        outcome: The current outcome.
        cleanup: Callable run against the outcome; may transform it.

    Returns:
        The cleanup's outcome, or a ``Failure`` folded from a raised
        ``Exception``.
    """
    held = outcome.value if isinstance(outcome, Success) else None
    return _fold(lambda: cleanup(outcome), held=held)


def from_status(
    response: Response,
    *,
    error_map: Mapping[int, type[HttpResponseError]] | None = None,
    max_body_bytes: int = _ERROR_BODY_CAP,
) -> Outcome[Response]:
    """Map a response's status band to an outcome.

    Only HTTP 400-599 is treated as an error status: a response in that band
    folds into a ``Failure`` carrying an ``HttpResponseError`` (a mapped
    subclass when ``error_map`` matches), with its body buffered up to
    ``max_body_bytes`` for post-mortem inspection inside a scope that still
    guarantees the response is closed even if the buffering itself fails. Any
    other status yields a ``Success`` carrying the still-open response.

    Args:
        response: The response to classify.
        error_map: Optional status-code to exception-subclass mapping.
        max_body_bytes: Cap on the buffered error-body preview.

    Returns:
        A ``Success`` for a non-error status, else a ``Failure``.
    """
    if not _is_error_status(int(response.status)):
        return Success(response)
    return Failure(_to_http_error(response, error_map, max_body_bytes))


def _is_error_status(status: int) -> bool:
    """Return whether ``status`` is in the HTTP 400-599 error band."""
    return 400 <= status < 600


def _fold(thunk: Callable[[], Outcome[Response]], held: Response | None) -> Outcome[Response]:
    """Run ``thunk``, folding an ``Exception`` into a ``Failure``.

    A ``BaseException`` (cancellation-class) propagates unconverted. On any
    failure a ``held`` open response is closed first, with a close failure
    chained onto the in-flight exception.
    """
    try:
        return thunk()
    except Exception as exc:
        if held is not None:
            _close_and_chain(held, exc)
        return Failure(exc)
    except BaseException as exc:
        if held is not None:
            _close_and_chain(held, exc)
        raise


def _close_and_chain(response: Response, primary: BaseException) -> None:
    """Close ``response``, chaining any close failure onto ``primary``.

    Guarantees the held response is released before ``primary`` continues to
    fold or propagate. A failure from ``close`` is attached to ``primary`` as a
    note rather than replacing it, so the original error is never lost.
    """
    try:
        response.close()
    except Exception as close_error:  # a close failure must not mask the primary
        primary.add_note(f"response close during error handling failed: {close_error!r}")


def _to_http_error(
    response: Response,
    error_map: Mapping[int, type[HttpResponseError]] | None,
    max_body_bytes: int,
) -> HttpResponseError:
    """Build the ``HttpResponseError`` for an error-status response."""
    buffered, read_error = _buffer_error_response(response, max_body_bytes)
    error = _select_error(buffered, error_map)
    if read_error is not None:
        error.add_note(f"error response body buffering failed: {read_error!r}")
    return error


def _select_error(
    response: Response,
    error_map: Mapping[int, type[HttpResponseError]] | None,
) -> HttpResponseError:
    """Pick the mapped error subclass for the status, else ``HttpResponseError``."""
    if error_map:
        mapped = error_map.get(int(response.status))
        if mapped is not None:
            return mapped(response=response)
    return HttpResponseError(response=response)


def _buffer_error_response(
    response: Response,
    max_body_bytes: int,
) -> tuple[Response, Exception | None]:
    """Buffer the error body up to the cap, guaranteeing the response closes.

    Returns the response re-bodied with a repeatable-read ``LoggableResponseBody``
    holding the captured prefix, plus any ``Exception`` raised while reading (the
    partial prefix is retained). A ``BaseException`` from the read propagates,
    but the ``finally`` still closes the response first.
    """
    body = response.body
    if body is None:
        return response, None
    try:
        prefix, read_error = _read_capped(body, max_body_bytes)
    finally:
        response.close()
    captured = ResponseBody.from_bytes(prefix)
    return response.with_body(LoggableResponseBody(captured)), read_error


def _read_capped(body: ResponseBody, cap: int) -> tuple[bytes, Exception | None]:
    """Read up to ``cap`` bytes from ``body``, retaining a partial read on failure."""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in body.iter_bytes():
            if total >= cap:
                break
            take = chunk[: cap - total]
            chunks.append(take)
            total += len(take)
    except Exception as exc:  # partial capture is retained for post-mortem
        return b"".join(chunks), exc
    return b"".join(chunks), None


__all__ = ["from_status", "map_success", "produce", "recover"]
