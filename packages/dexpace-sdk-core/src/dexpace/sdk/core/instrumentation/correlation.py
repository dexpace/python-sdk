# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Context-local trace/span correlation identifiers.

Holds the active trace id and span id in module-level ``contextvars`` so any
code on the same logical flow — including across ``await`` boundaries, which
asyncio propagates automatically — can read them without threading the values
through every call. The tracing policy sets them when it opens a span;
``ClientLogger`` reads them to stamp ``trace.id`` / ``span.id`` onto every log
record.

Code that hops to a worker thread (``loop.run_in_executor``) does not inherit
the caller's context automatically; use `bind_correlation` there to
re-establish the ids inside the worker.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import TYPE_CHECKING

from .tracing_scope import TracingScope

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextvars import Context, Token

    from .span import Span

_trace_id: ContextVar[str | None] = ContextVar("dexpace_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("dexpace_span_id", default=None)
_current_span: ContextVar[Span | None] = ContextVar("dexpace_current_span", default=None)


def get_trace_id() -> str | None:
    """Return the active trace id, or ``None`` when no trace is bound."""
    return _trace_id.get()


def get_span_id() -> str | None:
    """Return the active span id, or ``None`` when no span is bound."""
    return _span_id.get()


def set_trace_id(value: str | None) -> Token[str | None]:
    """Set the active trace id.

    Args:
        value: The trace id to bind, or ``None`` to clear it.

    Returns:
        A reset token; pass it to ``ContextVar.reset`` to restore the prior
        value. Prefer `bind_correlation` for scoped use.
    """
    return _trace_id.set(value)


def set_span_id(value: str | None) -> Token[str | None]:
    """Set the active span id.

    Args:
        value: The span id to bind, or ``None`` to clear it.

    Returns:
        A reset token; pass it to ``ContextVar.reset`` to restore the prior
        value. Prefer `bind_correlation` for scoped use.
    """
    return _span_id.set(value)


@contextmanager
def bind_correlation(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Iterator[None]:
    """Bind trace/span ids for the duration of the ``with`` block.

    Restores the previous ids on exit, even if the body raises. Both ids are
    always (re)bound for the block: an omitted (or explicitly ``None``)
    argument clears that id rather than leaving the inherited value in place.
    Pass the current value through if you only mean to override the other id.

    Args:
        trace_id: Trace id to bind for the block. Defaults to ``None``, which
            clears any inherited trace id for the block.
        span_id: Span id to bind for the block. Defaults to ``None``, which
            clears any inherited span id for the block.

    Yields:
        Nothing; use as a plain scope guard.
    """
    trace_token = _trace_id.set(trace_id)
    span_token = _span_id.set(span_id)
    try:
        yield
    finally:
        _span_id.reset(span_token)
        _trace_id.reset(trace_token)


def get_current_span() -> Span:
    """Return the ambient current span, or the no-op span when none is active.

    The active span is tracked in a ``contextvar``, so it flows across ``await``
    boundaries automatically and is captured by `capture_context` for
    cross-thread handoff. When no span has been activated the shared `NOOP_SPAN`
    is returned so callers never handle ``None``.
    """
    span = _current_span.get()
    if span is None:
        from .noop import NOOP_SPAN

        return NOOP_SPAN
    return span


def activate_span(span: Span) -> TracingScope:
    """Make ``span`` the ambient current span for the executing context.

    Returns a `TracingScope` whose ``close()`` restores the previously-current
    span — including when the scope body raises, because the restore runs in the
    scope's ``__exit__``. This is the machinery behind the default
    `Span.make_current`, so a backend span reuses the save/restore dance rather
    than re-implementing it.

    Args:
        span: The span to make current.

    Returns:
        A scope that restores the prior current span on close.
    """
    return _CurrentSpanScope(span)


class _CurrentSpanScope(TracingScope):
    """Restores the previously-current span when closed. ``close`` is idempotent."""

    __slots__ = ("_closed", "_token")

    def __init__(self, span: Span) -> None:
        self._token: Token[Span | None] = _current_span.set(span)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _current_span.reset(self._token)


@contextmanager
def correlation_scope(span: Span) -> Iterator[None]:
    """Bind ``span``'s ids for the block, skipping non-recording spans.

    For a recording span this binds ``trace.id`` / ``span.id`` from the span's
    `InstrumentationContext` so `ClientLogger` stamps them onto every record
    emitted while the block runs, restoring the prior ids on exit (including on
    exception). For a non-recording span it is a no-op: the inherited correlation
    ids are left untouched, because a span that is not recording must not
    overwrite the active correlation context.

    Args:
        span: The span whose ids drive correlation for the block.

    Yields:
        Nothing; use as a plain scope guard.
    """
    if not span.is_recording:
        yield
        return
    context = span.context
    with bind_correlation(trace_id=context.trace_id.value, span_id=context.span_id.value):
        yield


def capture_context() -> Context:
    """Return an immutable snapshot of the ambient context at the call site.

    Captured on the ORIGINATING side, at submission time. Re-run work inside it
    with ``snapshot.run(func)`` — typically on a worker thread — so the work
    observes the contextvars (including the correlation ids and current span) as
    they were when captured, rather than whatever a fresh thread inherited.
    ``Context.run`` isolates writes: a contextvar mutated inside the snapshot
    never leaks back to the caller's context.

    Returns:
        A ``contextvars.Context`` snapshot to run callables within.
    """
    return copy_context()


def bind_to_context[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Bind ``func`` to the ambient context captured NOW, at submission time.

    Snapshots the caller's contextvars at this call and returns a wrapper that
    runs ``func`` inside that snapshot when invoked — typically handed to
    ``Executor.submit`` so the worker observes the correlation context as it was
    at submission, not whatever the worker inherited or the caller later moved
    to. Mutations inside the wrapper stay isolated to the snapshot and never leak
    back to the caller.

    Args:
        func: The callable to rebind to the captured context.

    Returns:
        A wrapper that runs ``func`` within the captured context snapshot.
    """
    snapshot = copy_context()

    @functools.wraps(func)
    def _runner(*args: P.args, **kwargs: P.kwargs) -> T:
        return snapshot.run(func, *args, **kwargs)

    return _runner


__all__ = [
    "activate_span",
    "bind_correlation",
    "bind_to_context",
    "capture_context",
    "correlation_scope",
    "get_current_span",
    "get_span_id",
    "get_trace_id",
    "set_span_id",
    "set_trace_id",
]
