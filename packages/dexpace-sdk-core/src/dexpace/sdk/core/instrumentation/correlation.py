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

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextvars import Token

_trace_id: ContextVar[str | None] = ContextVar("dexpace_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("dexpace_span_id", default=None)


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


__all__ = [
    "bind_correlation",
    "get_span_id",
    "get_trace_id",
    "set_span_id",
    "set_trace_id",
]
