# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Structured logger wrapper for SDK-internal use."""

from __future__ import annotations

import logging
import threading
from typing import Any, Final

from .correlation import get_span_id, get_trace_id
from .log_level import LogLevel

_LEVEL_MAP: Final[dict[LogLevel, int]] = {
    LogLevel.ERROR: logging.ERROR,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.INFO: logging.INFO,
    LogLevel.VERBOSE: logging.DEBUG,
}

#: Serialises the check-then-act in ``_install_correlation_filter`` so two
#: threads constructing loggers concurrently cannot both add a filter. A single
#: process-wide lock is sufficient: installation is a one-time, low-contention
#: operation gated by an idempotent membership check.
_INSTALL_LOCK: Final[threading.Lock] = threading.Lock()


class CorrelationFilter(logging.Filter):
    """Stamps the active trace/span ids onto every record it sees.

    Reads the context-local ids from `correlation` and attaches them as
    ``trace.id`` / ``span.id`` record attributes (plus the dotted-name-safe
    ``trace_id`` / ``span_id`` aliases for ``%``-style format strings). When no
    trace is bound the attributes are set to ``None`` so formatters referencing
    them never raise. Because the ids live in ``contextvars``, this works across
    ``await`` boundaries without any extra plumbing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach correlation ids and always allow the record through."""
        trace_id = get_trace_id()
        span_id = get_span_id()
        setattr(record, "trace.id", trace_id)
        setattr(record, "span.id", span_id)
        record.trace_id = trace_id
        record.span_id = span_id
        return True


class ClientLogger:
    """Thin facade over stdlib ``logging`` that emits structured key=value pairs.

    Wraps a ``logging.Logger`` and prepends caller-supplied context (e.g.
    ``client="dexpace.foo"``) to every emission. Values are coerced via
    ``str()`` and quoted when they contain whitespace, matching the
    "logfmt" convention used by many observability backends.

    Attributes:
        name: The underlying logger name (also used for the log emitter).
    """

    __slots__ = ("_logger", "_static_fields", "name")

    def __init__(
        self,
        name: str,
        **static_fields: Any,
    ) -> None:
        """Configure the logger.

        Args:
            name: Logger name; passed to ``logging.getLogger``.
            **static_fields: Key=value pairs prepended to every emission.
        """
        self.name = name
        self._logger = logging.getLogger(name)
        self._static_fields = static_fields
        _install_correlation_filter(self._logger)

    def log(self, level: LogLevel, message: str, **fields: Any) -> None:
        """Emit a structured log record at ``level``."""
        py_level = _LEVEL_MAP[level]
        if not self._logger.isEnabledFor(py_level):
            return
        rendered = _format_fields({**self._static_fields, **_correlation_fields(), **fields})
        self._logger.log(py_level, "%s %s", message, rendered)

    def error(self, message: str, **fields: Any) -> None:
        """Emit a structured record at ``ERROR`` level."""
        self.log(LogLevel.ERROR, message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        """Emit a structured record at ``WARNING`` level."""
        self.log(LogLevel.WARNING, message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        """Emit a structured record at ``INFO`` level."""
        self.log(LogLevel.INFO, message, **fields)

    def verbose(self, message: str, **fields: Any) -> None:
        """Emit a structured record at ``VERBOSE`` (``DEBUG``) level."""
        self.log(LogLevel.VERBOSE, message, **fields)


def _install_correlation_filter(logger: logging.Logger) -> None:
    """Attach a `CorrelationFilter` to ``logger`` exactly once.

    The membership check and the ``addFilter`` call run under a process-wide
    lock so concurrent ``ClientLogger`` construction on the same logger can
    never install two filters (the check-then-act would otherwise race).
    """
    with _INSTALL_LOCK:
        if any(isinstance(existing, CorrelationFilter) for existing in logger.filters):
            return
        logger.addFilter(CorrelationFilter())


def _correlation_fields() -> dict[str, str]:
    """Return the bound trace/span ids as logfmt fields, omitting unset ones."""
    fields: dict[str, str] = {}
    trace_id = get_trace_id()
    if trace_id is not None:
        fields["trace.id"] = trace_id
    span_id = get_span_id()
    if span_id is not None:
        fields["span.id"] = span_id
    return fields


def _format_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        rendered = str(value)
        if any(c in rendered for c in ' \t"\n\r='):
            rendered = (
                '"'
                + rendered.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                + '"'
            )
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


__all__ = ["ClientLogger", "CorrelationFilter"]
