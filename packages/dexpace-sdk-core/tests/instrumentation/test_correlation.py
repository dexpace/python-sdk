# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for context-local trace/span correlation and log stamping."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.instrumentation import (
    ClientLogger,
    CorrelationFilter,
    bind_correlation,
    get_span_id,
    get_trace_id,
    set_span_id,
    set_trace_id,
)


@pytest.fixture(autouse=True)
def _clear_correlation() -> None:
    """Each test starts with no bound ids (contextvars default to ``None``)."""
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


def test_ids_propagate_across_await() -> None:
    async def _run() -> tuple[str | None, str | None]:
        with bind_correlation(trace_id="async-trace", span_id="async-span"):
            await asyncio.sleep(0)
            return get_trace_id(), get_span_id()

    trace_id, span_id = asyncio.run(_run())
    assert trace_id == "async-trace"
    assert span_id == "async-span"
