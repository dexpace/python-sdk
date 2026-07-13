# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for Span recording/end semantics and the current-span scope."""

from __future__ import annotations

from typing import Any

import pytest

from dexpace.sdk.core.instrumentation import (
    NOOP_INSTRUMENTATION_CONTEXT,
    NOOP_SPAN,
    NOOP_TRACER,
    InstrumentationContext,
    Span,
    TracingScope,
    get_current_span,
)


class _FakeSpan(Span):
    """Minimal recording span honouring the recording-flag + idempotent-end
    contract (OBS-21). Reuses the default `make_current` from the base ABC."""

    def __init__(self, context: InstrumentationContext | None = None) -> None:
        self._context = context or NOOP_INSTRUMENTATION_CONTEXT
        self.end_calls = 0
        self.recorded_error: BaseException | None = None

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def context(self) -> InstrumentationContext:
        return self._context

    def set_attribute(self, key: str, value: Any) -> _FakeSpan:
        return self

    def set_error(self, error_type: str) -> _FakeSpan:
        return self

    def end(self, error: BaseException | None = None) -> None:
        if self.end_calls == 0 and error is not None:
            self.recorded_error = error
        self.end_calls += 1


# ----- Recording flag + idempotent end (OBS-21) ----------------------------


def test_noop_span_is_not_recording() -> None:
    assert NOOP_SPAN.is_recording is False


def test_noop_span_end_is_idempotent() -> None:
    NOOP_SPAN.end()
    NOOP_SPAN.end()
    NOOP_SPAN.end(error=RuntimeError("boom"))


def test_noop_span_mutators_return_self() -> None:
    assert NOOP_SPAN.set_attribute("k", "v") is NOOP_SPAN
    assert NOOP_SPAN.set_error("ValueError") is NOOP_SPAN


def test_noop_span_context_is_the_noop_context() -> None:
    assert NOOP_SPAN.context is NOOP_INSTRUMENTATION_CONTEXT
    assert NOOP_SPAN.context.is_valid is False


def test_recording_span_end_records_error_once() -> None:
    span = _FakeSpan()
    err = RuntimeError("boom")
    span.end(error=err)
    span.end(error=RuntimeError("second"))
    assert span.end_calls == 2
    assert span.recorded_error is err


# ----- make_current restores the prior span, incl. on throw (OBS-22) -------


def test_default_current_span_is_the_noop_span() -> None:
    assert get_current_span() is NOOP_SPAN


def test_make_current_activates_and_restores_prior_span() -> None:
    outer = _FakeSpan()
    inner = _FakeSpan()
    assert get_current_span() is NOOP_SPAN
    with outer.make_current():
        assert get_current_span() is outer
        with inner.make_current():
            assert get_current_span() is inner
        assert get_current_span() is outer
    assert get_current_span() is NOOP_SPAN


def test_make_current_restores_prior_span_on_throw() -> None:
    outer = _FakeSpan()
    inner = _FakeSpan()
    with outer.make_current():
        assert get_current_span() is outer
        with pytest.raises(RuntimeError):  # noqa: SIM117 - nested scope under test
            with inner.make_current():
                assert get_current_span() is inner
                raise RuntimeError("boom")
        # The prior span is restored even though the body raised.
        assert get_current_span() is outer
    assert get_current_span() is NOOP_SPAN


def test_make_current_scope_close_is_idempotent() -> None:
    span = _FakeSpan()
    scope = span.make_current()
    assert get_current_span() is span
    scope.close()
    assert get_current_span() is NOOP_SPAN
    scope.close()  # second close is a documented no-op
    assert get_current_span() is NOOP_SPAN


def test_make_current_returns_a_tracing_scope() -> None:
    span = _FakeSpan()
    with span.make_current() as scope:
        assert isinstance(scope, TracingScope)


# ----- No-op allocation / identity (OBS-25) --------------------------------


def test_noop_span_make_current_reuses_the_shared_scope() -> None:
    first = NOOP_SPAN.make_current()
    second = NOOP_SPAN.make_current()
    assert first is second


def test_noop_span_make_current_does_not_touch_current_span() -> None:
    # A non-recording span never becomes the ambient current span.
    with NOOP_SPAN.make_current():
        assert get_current_span() is NOOP_SPAN


def test_noop_tracer_start_span_is_the_shared_noop_span() -> None:
    assert NOOP_TRACER.start_span("op") is NOOP_SPAN
    assert NOOP_TRACER.start_span("op", parent=NOOP_INSTRUMENTATION_CONTEXT) is NOOP_SPAN
