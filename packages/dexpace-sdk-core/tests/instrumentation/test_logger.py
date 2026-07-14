# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``ClientLogger`` structured emission."""

from __future__ import annotations

import logging
import threading
from typing import NoReturn

import pytest
from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.instrumentation import ClientLogger, LogEvent, LogLevel
from dexpace.sdk.core.instrumentation.client_logger import (
    _INSTRUMENTATION_ERROR_EVENT,
    _LEVEL_MAP,
    _NOOP_EVENT,
    _TRUNCATION_MARKER,
    _UNRENDERABLE,
    _format_fields,
    _render_value,
)


def _parse_fields(rendered: str) -> dict[str, str]:
    """Split a rendered logfmt string of unquoted tokens into a dict."""
    fields: dict[str, str] = {}
    for token in rendered.split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def test_emits_at_info(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.info")
    logger = ClientLogger("dexpace.test.info", client="test")
    logger.info("hello", trace_id="abc")

    assert any("hello" in rec.message for rec in caplog.records)
    rendered = caplog.records[-1].getMessage()
    assert "client=test" in rendered
    assert "trace_id=abc" in rendered


def test_quotes_values_with_whitespace(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="dexpace.test.quoted")
    logger = ClientLogger("dexpace.test.quoted")
    logger.warning("event", detail="hello world")

    rendered = caplog.records[-1].getMessage()
    assert 'detail="hello world"' in rendered


def test_format_fields_quotes_value_containing_equals() -> None:
    # A value embedding '=' is ambiguous to a logfmt parser unless quoted: the
    # parser cannot tell where the field's value ends and the next key begins.
    assert _format_fields({"k": "a=b"}) == 'k="a=b"'


def test_quotes_value_with_equals_through_logger(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.equals")
    logger = ClientLogger("dexpace.test.equals")
    logger.info("event", token="a=b")

    rendered = caplog.records[-1].getMessage()
    assert 'token="a=b"' in rendered


def test_plain_value_unquoted() -> None:
    # No quoting trigger present -> emitted bare.
    assert _format_fields({"k": "plain"}) == "k=plain"


def test_levels_dispatch_correctly(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="dexpace.test.levels")
    logger = ClientLogger("dexpace.test.levels")
    logger.error("e")
    logger.warning("w")
    logger.info("i")
    logger.verbose("v")
    logger.log(LogLevel.ERROR, "direct")

    levels = [rec.levelname for rec in caplog.records]
    assert levels.count("ERROR") == 2
    assert "WARNING" in levels
    assert "INFO" in levels
    assert "DEBUG" in levels


def test_skips_emission_when_level_disabled(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="dexpace.test.skip")
    logger = ClientLogger("dexpace.test.skip")
    logger.info("noisy")
    assert not [rec for rec in caplog.records if rec.message == "noisy"]


def test_newline_in_field_is_escaped(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.newline")
    logger = ClientLogger("dexpace.test.newline")
    logger.info("msg", note="line1\nline2")

    rendered = caplog.records[-1].getMessage()
    assert "\n" not in rendered
    assert "\\n" in rendered
    assert 'note="line1\\nline2"' in rendered


def test_carriage_return_escaped(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.cr")
    logger = ClientLogger("dexpace.test.cr")
    logger.info("msg", note="line1\r\nline2")

    rendered = caplog.records[-1].getMessage()
    assert "\r" not in rendered
    assert "\n" not in rendered
    assert "\\r" in rendered
    assert "\\n" in rendered
    assert 'note="line1\\r\\nline2"' in rendered


# --- OBS-2: four structured levels map onto stdlib levels --------------------


@pytest.mark.req("OBS-2")
def test_levels_map_to_stdlib_levels() -> None:
    assert _LEVEL_MAP == {
        LogLevel.ERROR: logging.ERROR,
        LogLevel.WARNING: logging.WARNING,
        LogLevel.INFO: logging.INFO,
        LogLevel.VERBOSE: logging.DEBUG,
    }


@pytest.mark.req("OBS-2")
def test_at_helpers_dispatch_to_matching_level(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="dexpace.test.at")
    logger = ClientLogger("dexpace.test.at")
    logger.at_error().log("e")
    logger.at_warning().log("w")
    logger.at_info().log("i")
    logger.at_verbose().log("v")

    levels = [rec.levelname for rec in caplog.records]
    assert levels == ["ERROR", "WARNING", "INFO", "DEBUG"]


# --- OBS-1: disabled level returns a shared inert event (zero allocation) -----


@pytest.mark.req("OBS-1")
def test_disabled_level_returns_shared_inert_singleton() -> None:
    logging.getLogger("dexpace.test.inert").setLevel(logging.ERROR)
    logger = ClientLogger("dexpace.test.inert")

    first = logger.at_info()
    second = logger.at_info()

    # Same object every time on the disabled path: no allocation.
    assert first is _NOOP_EVENT
    assert first is second


@pytest.mark.req("OBS-1")
def test_inert_event_chaining_stays_the_singleton_and_never_emits(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="dexpace.test.inert2")
    logging.getLogger("dexpace.test.inert2").setLevel(logging.ERROR)
    logger = ClientLogger("dexpace.test.inert2")

    event = logger.at_info()
    assert event.add("k", "v") is _NOOP_EVENT
    assert event.event("x") is _NOOP_EVENT
    event.log("nothing")  # inert: does not raise, does not emit

    assert not [rec for rec in caplog.records if "nothing" in rec.getMessage()]


# --- OBS-3: empty key rejected; None renders literal 'null' -------------------


@pytest.mark.req("OBS-3")
def test_empty_key_is_rejected(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="dexpace.test.emptykey")
    logger = ClientLogger("dexpace.test.emptykey")
    with pytest.raises(ValueError, match="non-empty"):
        logger.at_info().add("", "value")


def test_none_value_renders_null_literal() -> None:
    assert _render_value(None, 8192) == "null"


@pytest.mark.req("OBS-3")
def test_none_value_renders_null_through_logger(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.null")
    logger = ClientLogger("dexpace.test.null")
    logger.info("msg", token=None)

    assert "token=null" in caplog.records[-1].getMessage()


# --- OBS-4: reserved `event` tag emitted exactly once; empty clears -----------


@pytest.mark.req("OBS-4")
def test_reserved_event_tag_emitted_exactly_once(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.event")
    logger = ClientLogger("dexpace.test.event")
    logger.at_info().event("http.request").add("method", "GET").log("sent")

    rendered = caplog.records[-1].getMessage()
    assert rendered.count("event=") == 1
    assert "event=http.request" in rendered
    assert "method=GET" in rendered


@pytest.mark.req("OBS-4")
def test_reserved_event_empty_value_clears_tag(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.event2")
    logger = ClientLogger("dexpace.test.event2")
    logger.at_info().event("first").event("").add("k", "v").log("msg")

    rendered = caplog.records[-1].getMessage()
    assert "event=" not in rendered
    assert "k=v" in rendered


# --- OBS-5: field > global > diagnostic-context, one occurrence per key -------


@pytest.mark.req("OBS-5")
def test_precedence_field_over_global_over_diagnostic(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.prec")
    logger = ClientLogger(
        "dexpace.test.prec",
        diagnostic_allow_list=None,  # fold every ambient key
        diagnostic_context=lambda: {"a": "diag", "b": "diag", "c": "diag"},
        a="global",
        b="global",
    )
    logger.at_info().add("a", "field").log("")

    fields = _parse_fields(caplog.records[-1].getMessage())
    assert fields == {"a": "field", "b": "global", "c": "diag"}


# --- OBS-6: total rendering — a throwing __str__ yields a placeholder ---------


class _BoomStr:
    def __str__(self) -> str:
        raise RuntimeError("boom")


class _BoomBaseStr:
    def __str__(self) -> str:
        raise KeyboardInterrupt("boom")


@pytest.mark.req("OBS-6", "XCUT-20")
def test_throwing_str_yields_placeholder(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.boom")
    logger = ClientLogger("dexpace.test.boom")
    logger.at_info().add("k", _BoomStr()).log("msg")  # must not raise

    assert f"k={_UNRENDERABLE}" in caplog.records[-1].getMessage()


def test_base_exception_from_str_propagates(caplog: LogCaptureFixture) -> None:
    # OBS-20 boundary: the logger swallows Exception for rendering, but a
    # BaseException (the propagate contract that also governs tracer/meter
    # callbacks) is never swallowed by its own error handling.
    caplog.set_level(logging.INFO, logger="dexpace.test.boombase")
    logger = ClientLogger("dexpace.test.boombase")
    with pytest.raises(KeyboardInterrupt):
        logger.at_info().add("k", _BoomBaseStr()).log("msg")


# --- OBS-7: bounded truncation, primitives exempt ----------------------------


@pytest.mark.req("OBS-7")
def test_long_value_is_truncated() -> None:
    rendered = _render_value("x" * 100, 10)
    assert rendered == "x" * 10 + _TRUNCATION_MARKER
    assert "x" * 11 not in rendered


@pytest.mark.req("OBS-6")
def test_primitives_are_exempt_from_truncation() -> None:
    assert _render_value(123456789, 3) == "123456789"
    assert _render_value(True, 1) == "True"
    assert _render_value(10**50, 3) == str(10**50)
    assert _TRUNCATION_MARKER not in _render_value(10**50, 3)


@pytest.mark.req("OBS-7")
def test_truncation_bounds_value_through_logger(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.trunc")
    logger = ClientLogger("dexpace.test.trunc", max_field_length=8)
    logger.info("msg", blob="y" * 200)

    rendered = caplog.records[-1].getMessage()
    assert _TRUNCATION_MARKER in rendered
    assert "y" * 200 not in rendered
    assert "y" * 8 + _TRUNCATION_MARKER in rendered


# --- OBS-8: emit-once latch correct under real thread concurrency ------------


@pytest.mark.req("OBS-8", "XCUT-11")
def test_emit_once_latch_under_thread_race(caplog: LogCaptureFixture) -> None:
    # Designed to hold under free-threaded CPython too: the latch is guarded by
    # an explicit threading.Lock, not the GIL.
    caplog.set_level(logging.INFO, logger="dexpace.test.race")
    logger = ClientLogger("dexpace.test.race")
    event = logger.at_info().add("k", "v")

    thread_count = 64
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()  # maximise contention on the single event
        event.log("race")

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    emitted = [rec for rec in caplog.records if "race" in rec.getMessage()]
    assert len(emitted) == 1


# --- OBS-9: global context attaches to every event --------------------------


@pytest.mark.req("OBS-9")
def test_global_context_on_every_event(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.global")
    logger = ClientLogger("dexpace.test.global", client="dexpace.foo", region="eu")
    logger.at_info().log("first")
    logger.at_info().add("extra", "1").log("second")

    for rec in caplog.records:
        rendered = rec.getMessage()
        assert "client=dexpace.foo" in rendered
        assert "region=eu" in rendered


# --- OBS-10: diagnostic-context allow-list -----------------------------------


@pytest.mark.req("OBS-10")
def test_diagnostic_allow_list_defaults_to_trace_and_span(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.diag")
    logger = ClientLogger(
        "dexpace.test.diag",
        diagnostic_context=lambda: {"trace.id": "T", "span.id": "S", "secret": "X"},
    )
    logger.at_info().log("msg")

    rendered = caplog.records[-1].getMessage()
    assert "trace.id=T" in rendered
    assert "span.id=S" in rendered
    assert "secret" not in rendered  # not allow-listed by default


@pytest.mark.req("OBS-10")
def test_diagnostic_allow_list_none_folds_all(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.diagall")
    logger = ClientLogger(
        "dexpace.test.diagall",
        diagnostic_allow_list=None,
        diagnostic_context=lambda: {"trace.id": "T", "custom": "C"},
    )
    logger.at_info().log("msg")

    rendered = caplog.records[-1].getMessage()
    assert "trace.id=T" in rendered
    assert "custom=C" in rendered


@pytest.mark.req("OBS-10")
def test_diagnostic_null_values_are_skipped(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.diagnull")
    logger = ClientLogger(
        "dexpace.test.diagnull",
        diagnostic_context=lambda: {"trace.id": None, "span.id": "S"},
    )
    logger.at_info().log("msg")

    rendered = caplog.records[-1].getMessage()
    assert "trace.id" not in rendered  # null value skipped entirely
    assert "span.id=S" in rendered


# --- OBS-20: logging failures re-emitted; tracer/meter throws propagate -------


def _broken_provider() -> NoReturn:
    raise RuntimeError("provider down")


@pytest.mark.req("OBS-20", "XCUT-20")
def test_logging_failure_reemitted_as_instrumentation_error(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="dexpace.test.fail")
    logger = ClientLogger("dexpace.test.fail", diagnostic_context=_broken_provider)

    logger.info("http.request", method="GET")  # a logging failure must not crash

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(_INSTRUMENTATION_ERROR_EVENT in msg for msg in messages)
    assert any("error_type=RuntimeError" in msg for msg in messages)


# --- OBS-40: reserved-key collision warns once per logger --------------------


@pytest.mark.req("OBS-40")
def test_reserved_key_collision_warns_once_per_logger(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="dexpace.test.collide")
    logger = ClientLogger("dexpace.test.collide")

    logger.info("m1", event="a")
    logger.info("m2", event="b")
    logger.at_info().add("event", "c").log("m3")

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "reserved" in rec.getMessage()
    ]
    assert len(warnings) == 1


# --- API surface / typing sanity ---------------------------------------------


def test_active_event_is_a_logevent(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dexpace.test.api")
    logger = ClientLogger("dexpace.test.api")
    event = logger.at_info()
    assert isinstance(event, LogEvent)


def test_static_fields_still_supported(caplog: LogCaptureFixture) -> None:
    # Backwards-compatible: positional name plus **global_context kwargs.
    caplog.set_level(logging.INFO, logger="dexpace.test.compat")
    logger = ClientLogger("dexpace.test.compat", client="test")
    logger.info("hello", trace_id="abc")

    rendered = caplog.records[-1].getMessage()
    assert "client=test" in rendered
    assert "trace_id=abc" in rendered
