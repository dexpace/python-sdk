# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for context-local trace/span correlation and log stamping."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator

import pytest
from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.instrumentation import (
    NOOP_SPAN,
    ClientLogger,
    CorrelationFilter,
    InstrumentationContext,
    Span,
    SpanId,
    TraceId,
    TraceIdType,
    activate_span,
    bind_correlation,
    bind_to_context,
    capture_context,
    correlation_scope,
    get_current_span,
    get_span_id,
    get_trace_id,
    set_span_id,
    set_trace_id,
)


class _FakeRecordingSpan(Span):
    """A recording span carrying a fixed context, for correlation-scope tests."""

    def __init__(self, context: InstrumentationContext) -> None:
        self._context = context

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def context(self) -> InstrumentationContext:
        return self._context

    def set_attribute(self, key: str, value: object) -> _FakeRecordingSpan:
        return self

    def set_error(self, error_type: str) -> _FakeRecordingSpan:
        return self

    def end(self, error: BaseException | None = None) -> None:
        return None


def _recording_context(trace: str, span: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId(span),
        span=NOOP_SPAN,
    )


@pytest.fixture(autouse=True)
def _clear_correlation() -> Iterator[None]:
    """Isolate each test's correlation state.

    Clears the trace/span ids before the test so it starts with the contextvar
    defaults (``None``), and clears them again afterwards so a test that binds
    ids on the main thread never leaks that state into other test modules.
    """
    set_trace_id(None)
    set_span_id(None)
    yield
    set_trace_id(None)
    set_span_id(None)


def test_getters_default_to_none() -> None:
    assert get_trace_id() is None
    assert get_span_id() is None


def test_set_and_get_roundtrip() -> None:
    set_trace_id("trace-1")
    set_span_id("span-1")
    assert get_trace_id() == "trace-1"
    assert get_span_id() == "span-1"


def test_set_returns_token_that_resets() -> None:
    set_trace_id("outer")
    token = set_trace_id("inner")
    assert get_trace_id() == "inner"
    token.var.reset(token)
    assert get_trace_id() == "outer"


@pytest.mark.req("TRACING-01")
def test_bind_correlation_scopes_and_restores() -> None:
    set_trace_id("outer-trace")
    set_span_id("outer-span")
    with bind_correlation(trace_id="inner-trace", span_id="inner-span"):
        assert get_trace_id() == "inner-trace"
        assert get_span_id() == "inner-span"
    assert get_trace_id() == "outer-trace"
    assert get_span_id() == "outer-span"


def test_bind_correlation_restores_on_exception() -> None:
    set_trace_id("outer")
    with pytest.raises(RuntimeError), bind_correlation(trace_id="inner"):
        assert get_trace_id() == "inner"
        raise RuntimeError("boom")
    assert get_trace_id() == "outer"


def test_logger_stamps_bound_ids_into_message(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.corr.msg")
    logger = ClientLogger("dexpace.test.corr.msg")
    with bind_correlation(trace_id="t-42", span_id="s-7"):
        logger.info("request")

    rendered = caplog.records[-1].getMessage()
    assert "trace.id=t-42" in rendered
    assert "span.id=s-7" in rendered


def test_logger_omits_unset_ids_from_message(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.corr.unset")
    logger = ClientLogger("dexpace.test.corr.unset")
    logger.info("request")

    rendered = caplog.records[-1].getMessage()
    assert "trace.id=" not in rendered
    assert "span.id=" not in rendered


def test_filter_sets_record_attributes(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.corr.attr")
    logger = ClientLogger("dexpace.test.corr.attr")
    with bind_correlation(trace_id="trace-x", span_id="span-y"):
        logger.info("event")

    record = caplog.records[-1]
    assert record.trace_id == "trace-x"  # type: ignore[attr-defined]
    assert record.span_id == "span-y"  # type: ignore[attr-defined]
    assert getattr(record, "trace.id") == "trace-x"
    assert getattr(record, "span.id") == "span-y"


def test_filter_sets_none_when_unbound(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.corr.none")
    logger = ClientLogger("dexpace.test.corr.none")
    logger.info("event")

    record = caplog.records[-1]
    assert record.trace_id is None  # type: ignore[attr-defined]
    assert record.span_id is None  # type: ignore[attr-defined]


def test_correlation_filter_installed_once() -> None:
    name = "dexpace.test.corr.once"
    ClientLogger(name)
    ClientLogger(name)
    installed = [f for f in logging.getLogger(name).filters if isinstance(f, CorrelationFilter)]
    assert len(installed) == 1


def test_correlation_filter_installed_once_under_concurrency() -> None:
    # Many threads constructing a logger for the same name concurrently must
    # still end up with exactly one CorrelationFilter: the install guard runs
    # the check-then-act under a lock, so the race cannot duplicate it.
    name = "dexpace.test.corr.concurrent"
    barrier = threading.Barrier(16)

    def _build() -> None:
        barrier.wait()
        ClientLogger(name)

    threads = [threading.Thread(target=_build) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    installed = [f for f in logging.getLogger(name).filters if isinstance(f, CorrelationFilter)]
    assert len(installed) == 1


@pytest.mark.req("TRACING-01")
def test_ids_propagate_across_await() -> None:
    async def _run() -> tuple[str | None, str | None]:
        with bind_correlation(trace_id="async-trace", span_id="async-span"):
            await asyncio.sleep(0)
            return get_trace_id(), get_span_id()

    trace_id, span_id = asyncio.run(_run())
    assert trace_id == "async-trace"
    assert span_id == "async-span"


# ----- contextvars bridge across threads (OBS-24, ASYNC-8..12) --------------


@pytest.mark.req("OBS-24")
def test_capture_context_propagates_submission_snapshot_across_threads() -> None:
    # Capture on the ORIGINATING side (submission time), reinstall on a worker
    # thread. The worker observes the value as it was AT CAPTURE, not the value
    # the calling thread later moved to, and a fresh thread would otherwise see
    # only the ``None`` default.
    set_trace_id("at-submission")
    snapshot = capture_context()
    set_trace_id("after-submission")

    seen: dict[str, str | None] = {}
    started = threading.Event()
    may_read = threading.Event()

    def worker() -> None:
        started.set()
        may_read.wait()  # read strictly AFTER the caller changed the id
        seen["trace"] = get_trace_id()
        set_trace_id("mutated-in-worker")  # must not leak back to the caller

    thread = threading.Thread(target=lambda: snapshot.run(worker))
    thread.start()
    started.wait()
    may_read.set()
    thread.join()

    assert seen["trace"] == "at-submission"
    assert get_trace_id() == "after-submission"


def test_bind_to_context_captures_at_submission_not_at_thread_start() -> None:
    set_trace_id("submission")
    bound = bind_to_context(_read_then_mutate)  # snapshot captured HERE
    set_trace_id("after")  # changed before the worker even starts

    result: list[str | None] = []
    thread = threading.Thread(target=lambda: result.append(bound()))
    thread.start()
    thread.join()

    assert result == ["submission"]  # snapshot value, not "after"
    assert get_trace_id() == "after"  # worker mutation stayed isolated


def _read_then_mutate() -> str | None:
    observed = get_trace_id()
    set_trace_id("mutated-in-worker")
    return observed


def test_capture_context_isolates_mutations_from_caller() -> None:
    set_trace_id("caller")
    snapshot = capture_context()

    def mutate() -> None:
        set_trace_id("inside-snapshot")
        assert get_trace_id() == "inside-snapshot"

    snapshot.run(mutate)
    assert get_trace_id() == "caller"


# ----- current span scope (OBS-22) ------------------------------------------


def test_get_current_span_defaults_to_noop() -> None:
    assert get_current_span() is NOOP_SPAN


def test_activate_span_restores_prior_span_including_on_throw() -> None:
    outer = _FakeRecordingSpan(_recording_context("t-o", "s-o"))
    inner = _FakeRecordingSpan(_recording_context("t-i", "s-i"))
    with activate_span(outer):
        assert get_current_span() is outer
        with pytest.raises(RuntimeError), activate_span(inner):
            assert get_current_span() is inner
            raise RuntimeError("boom")
        assert get_current_span() is outer
    assert get_current_span() is NOOP_SPAN


# ----- log-correlation scope, skipped for non-recording spans (OBS-23) ------


def test_correlation_scope_binds_recording_span_ids() -> None:
    span = _FakeRecordingSpan(_recording_context("rec-trace", "rec-span"))
    set_trace_id("outer-t")
    set_span_id("outer-s")
    with correlation_scope(span):
        assert get_trace_id() == "rec-trace"
        assert get_span_id() == "rec-span"
    assert get_trace_id() == "outer-t"
    assert get_span_id() == "outer-s"


def test_correlation_scope_restores_ids_on_throw() -> None:
    span = _FakeRecordingSpan(_recording_context("rec-trace", "rec-span"))
    set_trace_id("outer-t")
    set_span_id("outer-s")
    with pytest.raises(RuntimeError), correlation_scope(span):
        assert get_trace_id() == "rec-trace"
        raise RuntimeError("boom")
    assert get_trace_id() == "outer-t"
    assert get_span_id() == "outer-s"


def test_correlation_scope_is_noop_for_non_recording_span() -> None:
    # A span that isn't recording must NOT overwrite the ambient ids: the
    # inherited correlation context is left untouched for the block.
    set_trace_id("inherited-t")
    set_span_id("inherited-s")
    with correlation_scope(NOOP_SPAN):
        assert get_trace_id() == "inherited-t"
        assert get_span_id() == "inherited-s"
    assert get_trace_id() == "inherited-t"
    assert get_span_id() == "inherited-s"
