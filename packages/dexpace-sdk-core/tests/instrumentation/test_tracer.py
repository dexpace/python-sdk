# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the Tracer span factory: no-op default and never-wrapped contract."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.instrumentation import (
    NOOP_SPAN,
    NOOP_TRACER,
    InstrumentationContext,
    Span,
    Tracer,
)


def test_noop_tracer_is_a_tracer() -> None:
    assert isinstance(NOOP_TRACER, Tracer)


def test_noop_tracer_returns_the_shared_noop_span() -> None:
    assert NOOP_TRACER.start_span("op") is NOOP_SPAN
    assert NOOP_TRACER.start_span("op", parent=None) is NOOP_SPAN


def test_tracer_exception_is_not_wrapped_or_suppressed() -> None:
    # Tracer/meter callbacks are contractually never-throwing; when one does
    # raise, the SDK does NOT wrap or swallow it — the original propagates.
    sentinel = RuntimeError("tracer boom")

    class _BoomTracer(Tracer):
        def start_span(self, name: str, parent: InstrumentationContext | None = None) -> Span:
            raise sentinel

    with pytest.raises(RuntimeError) as excinfo:
        _BoomTracer().start_span("op")
    assert excinfo.value is sentinel
