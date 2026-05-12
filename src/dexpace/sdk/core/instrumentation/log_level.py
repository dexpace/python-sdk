"""Log levels used by SDK loggers."""
from __future__ import annotations

from enum import Enum


class LogLevel(Enum):
    """Severity levels, ordered most-severe to most-verbose.

    Maps to the standard ``logging`` module levels: ``VERBOSE`` corresponds to
    ``logging.DEBUG``, ``INFO`` to ``logging.INFO``, ``WARNING`` to
    ``logging.WARNING``, ``ERROR`` to ``logging.ERROR``.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"
    VERBOSE = "VERBOSE"


__all__ = ["LogLevel"]
