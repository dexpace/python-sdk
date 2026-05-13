"""Tests for ``ClientLogger`` structured emission."""

from __future__ import annotations

import logging

from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.instrumentation import ClientLogger, LogLevel


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
