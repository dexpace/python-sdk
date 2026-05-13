"""Structured logger wrapper for SDK-internal use."""

from __future__ import annotations

import logging
from typing import Any, Final

from .log_level import LogLevel

_LEVEL_MAP: Final[dict[LogLevel, int]] = {
    LogLevel.ERROR: logging.ERROR,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.INFO: logging.INFO,
    LogLevel.VERBOSE: logging.DEBUG,
}


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

    def log(self, level: LogLevel, message: str, **fields: Any) -> None:
        """Emit a structured log record at ``level``."""
        py_level = _LEVEL_MAP[level]
        if not self._logger.isEnabledFor(py_level):
            return
        self._logger.log(
            py_level, "%s %s", message, _format_fields({**self._static_fields, **fields})
        )

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


def _format_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        rendered = str(value)
        if any(c in rendered for c in ' \t"\n\r'):
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


__all__ = ["ClientLogger"]
