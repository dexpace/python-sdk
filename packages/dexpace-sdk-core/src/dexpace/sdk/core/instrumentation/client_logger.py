# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Structured logger wrapper for SDK-internal use.

Wraps a stdlib ``logging.Logger`` behind a fluent, structured event API. The
primary surface is ``ClientLogger.at_<level>()`` which returns a `LogEvent`
builder; callers chain ``add`` / ``event`` and terminate with ``log``:

    logger.at_info().event("http.request").add("method", "GET").log("sent")

Design guarantees:

- **Zero-allocation disabled path.** When a level is disabled the ``at_*``
  helpers return a single shared inert `LogEvent` (identity-stable); nothing is
  rendered or allocated.
- **Total rendering.** A value whose ``__str__`` raises yields a placeholder
  rather than propagating; ``None`` renders as the literal ``null``; values are
  bounded by a configurable length (primitives exempt).
- **Emit-once.** Each active event emits at most once, guarded by an explicit
  ``threading.Lock`` (no reliance on the GIL / atomic ops).
- **Precedence.** Field values override global-context values, which override
  ambient diagnostic-context values; each key appears at most once.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Final, Self

from .correlation import get_span_id, get_trace_id
from .log_level import LogLevel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LEVEL_MAP: Final[dict[LogLevel, int]] = {
    LogLevel.ERROR: logging.ERROR,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.INFO: logging.INFO,
    LogLevel.VERBOSE: logging.DEBUG,
}

#: Reserved structured key. It is set only through `LogEvent.event`; a plain
#: ``add`` targeting it is a collision (warned once per logger, then dropped).
_EVENT_KEY: Final = "event"
_RESERVED_KEYS: Final[frozenset[str]] = frozenset({_EVENT_KEY})

#: Placeholder substituted for a value whose ``str()`` raises.
_UNRENDERABLE: Final = "<unrenderable>"

#: Literal rendered for a ``None`` field value.
_NULL: Final = "null"

#: Appended when a (non-primitive) value is truncated.
_TRUNCATION_MARKER: Final = "...[truncated]"

#: Default per-value character budget before truncation kicks in.
_DEFAULT_MAX_FIELD_LENGTH: Final = 8192

#: Ambient diagnostic keys folded into every event by default.
_DEFAULT_DIAGNOSTIC_ALLOW_LIST: Final[frozenset[str]] = frozenset({"trace.id", "span.id"})

#: Event tag used when the logger re-emits its own emission failure.
_INSTRUMENTATION_ERROR_EVENT: Final = "http.instrumentation.error"

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


class LogEvent(ABC):
    """A single structured log emission under construction.

    Obtain one from ``ClientLogger.at_<level>()``. ``add`` / ``event`` return
    the same builder for chaining; ``log`` terminates the event and emits it at
    most once. The instance returned for a disabled level is a shared inert
    singleton (see `ClientLogger.at_level`).
    """

    __slots__ = ()

    @abstractmethod
    def add(self, key: str, value: Any) -> Self:
        """Attach a structured field.

        Args:
            key: Non-empty field name.
            value: Field value; rendered lazily at ``log`` time.

        Returns:
            This builder, for chaining.
        """

    @abstractmethod
    def event(self, name: str) -> Self:
        """Set the reserved ``event`` tag.

        Args:
            name: Event identifier; an empty string clears any previously set
                tag so no ``event`` field is emitted.

        Returns:
            This builder, for chaining.
        """

    @abstractmethod
    def log(self, message: str = "") -> None:
        """Render and emit the event exactly once.

        Args:
            message: Optional human-readable message rendered before the
                structured fields.
        """


class _ActiveLogEvent(LogEvent):
    """A `LogEvent` bound to an enabled level; renders and emits on ``log``."""

    __slots__ = ("_client", "_emitted", "_event_tag", "_fields", "_level", "_lock")

    def __init__(self, client: ClientLogger, level: LogLevel) -> None:
        self._client = client
        self._level = level
        self._fields: dict[str, Any] = {}
        self._event_tag: str | None = None
        self._lock = threading.Lock()
        self._emitted = False

    def add(self, key: str, value: Any) -> Self:
        """Record a field, rejecting empty keys and reserved-key collisions."""
        if not key:
            raise ValueError("log field key must be a non-empty string")
        if key in _RESERVED_KEYS:
            self._client._warn_reserved_key(key)
            return self
        self._fields[key] = value
        return self

    def event(self, name: str) -> Self:
        """Set (or, with an empty string, clear) the reserved ``event`` tag."""
        self._event_tag = name
        return self

    def log(self, message: str = "") -> None:
        """Emit once; on emission failure re-emit an internal diagnostic."""
        with self._lock:
            if self._emitted:
                return
            self._emitted = True
        try:
            self._client._emit(self._level, message, self._event_tag, self._fields)
        except Exception as exc:  # a logging failure must not crash the caller
            self._client._report_instrumentation_failure(exc)


class _NoopLogEvent(LogEvent):
    """Inert `LogEvent` returned for disabled levels; every method is a no-op."""

    __slots__ = ()

    def add(self, key: str, value: Any) -> Self:
        """Ignore the field and return the shared singleton for chaining."""
        return self

    def event(self, name: str) -> Self:
        """Ignore the tag and return the shared singleton for chaining."""
        return self

    def log(self, message: str = "") -> None:
        """Do nothing; the level is disabled."""


#: Shared inert event returned whenever a level is disabled. Returning a single
#: instance keeps the disabled path allocation-free and identity-stable.
_NOOP_EVENT: Final[_NoopLogEvent] = _NoopLogEvent()


class ClientLogger:
    """Thin facade over stdlib ``logging`` that emits structured key=value pairs.

    Wraps a ``logging.Logger`` and folds three field sources into every event,
    highest precedence first: per-event fields, this logger's global context,
    and the ambient diagnostic context (trace/span ids by default). Values are
    coerced via ``str()``, ``None`` renders as ``null``, and any token needing
    it is quoted, matching the "logfmt" convention used by many observability
    backends.

    The logger guards its own emission: a failure while rendering or handing a
    record to stdlib ``logging`` is caught and re-emitted as an
    ``http.instrumentation.error`` diagnostic rather than propagated. It does
    not, however, wrap any tracer/meter callback, so exceptions from those
    (which by contract must surface) are never swallowed here.

    Attributes:
        name: The underlying logger name (also used for the log emitter).
    """

    __slots__ = (
        "_diagnostic_allow_list",
        "_diagnostic_context",
        "_global_context",
        "_logger",
        "_max_field_length",
        "_reserved_lock",
        "_reserved_warned",
        "name",
    )

    def __init__(
        self,
        name: str,
        *,
        max_field_length: int = _DEFAULT_MAX_FIELD_LENGTH,
        diagnostic_allow_list: frozenset[str] | None = _DEFAULT_DIAGNOSTIC_ALLOW_LIST,
        diagnostic_context: Callable[[], Mapping[str, Any]] | None = None,
        **global_context: Any,
    ) -> None:
        """Configure the logger.

        Args:
            name: Logger name; passed to ``logging.getLogger``.
            max_field_length: Character budget applied to each rendered value;
                primitive values (``int`` / ``float`` / ``bool``) are exempt.
            diagnostic_allow_list: Ambient diagnostic keys folded into events.
                Defaults to ``{"trace.id", "span.id"}``; pass ``None`` to fold
                every ambient key; ambient keys whose value is ``None`` are
                always skipped.
            diagnostic_context: Callable returning the ambient diagnostic
                fields; defaults to the bound trace/span ids.
            **global_context: Key=value pairs folded into every emission.
        """
        self.name = name
        self._logger = logging.getLogger(name)
        self._global_context = global_context
        self._max_field_length = max_field_length
        self._diagnostic_allow_list = diagnostic_allow_list
        self._diagnostic_context = diagnostic_context or _default_diagnostic_context
        self._reserved_lock = threading.Lock()
        self._reserved_warned = False
        _install_correlation_filter(self._logger)

    def is_enabled(self, level: LogLevel) -> bool:
        """Return whether ``level`` would be emitted by the underlying logger."""
        return self._logger.isEnabledFor(_LEVEL_MAP[level])

    def at_level(self, level: LogLevel) -> LogEvent:
        """Start an event at ``level``.

        Returns:
            A fresh active builder when the level is enabled, otherwise the
            shared inert singleton (same object every time) so the disabled
            path allocates nothing.
        """
        if not self.is_enabled(level):
            return _NOOP_EVENT
        return _ActiveLogEvent(self, level)

    def at_error(self) -> LogEvent:
        """Start an event at ``ERROR`` level."""
        return self.at_level(LogLevel.ERROR)

    def at_warning(self) -> LogEvent:
        """Start an event at ``WARNING`` level."""
        return self.at_level(LogLevel.WARNING)

    def at_info(self) -> LogEvent:
        """Start an event at ``INFO`` level."""
        return self.at_level(LogLevel.INFO)

    def at_verbose(self) -> LogEvent:
        """Start an event at ``VERBOSE`` (``DEBUG``) level."""
        return self.at_level(LogLevel.VERBOSE)

    def log(self, level: LogLevel, message: str, **fields: Any) -> None:
        """Emit a structured log record at ``level``.

        Convenience wrapper over the builder API; disabled levels short-circuit
        on the shared inert event.
        """
        event = self.at_level(level)
        for key, value in fields.items():
            event.add(key, value)
        event.log(message)

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

    def _emit(
        self,
        level: LogLevel,
        message: str,
        event_tag: str | None,
        fields: Mapping[str, Any],
    ) -> None:
        """Render the merged fields and hand the record to stdlib logging."""
        ordered: dict[str, Any] = {}
        if event_tag:
            ordered[_EVENT_KEY] = event_tag
        for key, value in self._merge(fields).items():
            if key not in _RESERVED_KEYS:
                ordered[key] = value
        rendered = _format_fields(ordered, self._max_field_length)
        py_level = _LEVEL_MAP[level]
        if message:
            self._logger.log(py_level, "%s %s", message, rendered)
        else:
            self._logger.log(py_level, "%s", rendered)

    def _merge(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Fold the three sources: field > global > diagnostic-context."""
        return {**self._diagnostic_fields(), **self._global_context, **fields}

    def _diagnostic_fields(self) -> dict[str, Any]:
        """Return allow-listed ambient diagnostic fields, dropping null values."""
        allow = self._diagnostic_allow_list
        result: dict[str, Any] = {}
        for key, value in self._diagnostic_context().items():
            if value is None:
                continue
            if allow is not None and key not in allow:
                continue
            result[key] = value
        return result

    def _warn_reserved_key(self, key: str) -> None:
        """Warn once per logger about a field colliding with a reserved key."""
        with self._reserved_lock:
            if self._reserved_warned:
                return
            self._reserved_warned = True
        self._logger.warning("log field key %r is reserved; set it via .event()", key)

    def _report_instrumentation_failure(self, exc: BaseException) -> None:
        """Re-emit an emission failure as an internal diagnostic, never raising.

        Bypasses the (possibly failing) diagnostic-context provider and global
        context to keep this last-resort path from re-triggering the fault.
        """
        with contextlib.suppress(Exception):
            self._logger.log(
                logging.ERROR,
                "%s",
                f"{_EVENT_KEY}={_INSTRUMENTATION_ERROR_EVENT} error_type={type(exc).__name__}",
            )


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


def _default_diagnostic_context() -> dict[str, str | None]:
    """Return the bound trace/span ids as the ambient diagnostic context."""
    return {"trace.id": get_trace_id(), "span.id": get_span_id()}


def _format_fields(
    fields: Mapping[str, Any],
    max_field_length: int = _DEFAULT_MAX_FIELD_LENGTH,
) -> str:
    """Render ``fields`` as a space-separated logfmt string.

    Rendering is total: no value can raise. ``None`` becomes ``null``, values
    are truncated to ``max_field_length`` (primitives exempt), and any token
    containing whitespace, quotes, or ``=`` is quoted with escapes.
    """
    return " ".join(
        f"{key}={_render_value(value, max_field_length)}" for key, value in fields.items()
    )


def _render_value(value: Any, max_field_length: int) -> str:
    """Render a single value to its logfmt token (never raises)."""
    if value is None:
        return _NULL
    text = _safe_str(value)
    if not isinstance(value, (int, float)):  # bool is an int subclass -> exempt
        text = _truncate(text, max_field_length)
    return _quote(text)


def _safe_str(value: Any) -> str:
    """Coerce ``value`` to ``str``, substituting a placeholder if it raises.

    Only ``Exception`` is caught; a ``BaseException`` (e.g. ``KeyboardInterrupt``)
    raised from ``__str__`` still propagates.
    """
    try:
        return str(value)
    except Exception:
        return _UNRENDERABLE


def _truncate(text: str, max_field_length: int) -> str:
    """Cap ``text`` at ``max_field_length`` chars, appending a marker if cut."""
    if len(text) <= max_field_length:
        return text
    return text[:max_field_length] + _TRUNCATION_MARKER


def _quote(text: str) -> str:
    """Quote ``text`` for logfmt when it contains whitespace, quotes, or ``=``."""
    if any(c in text for c in ' \t"\n\r='):
        return (
            '"'
            + text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            + '"'
        )
    return text


__all__ = ["ClientLogger", "CorrelationFilter", "LogEvent"]
