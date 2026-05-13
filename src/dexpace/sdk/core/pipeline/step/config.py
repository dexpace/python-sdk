"""Optional configuration objects layered onto pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StepMetadata:
    """Human-readable identification for a pipeline step.

    Used by logging, tracing, and tooling that needs to identify a step at
    runtime. Bump :attr:`version` when behavior changes in a non
    back-compatible way.
    """

    name: str
    description: str = ""
    version: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Configuration describing how a step should be retried on failure.

    Defaults provide a reasonable fixed-delay policy with exponential backoff
    disabled; override individual fields to tune. ``retry_on`` defaults to
    transient I/O failures (:class:`OSError`, :class:`TimeoutError`) —
    programmer-error exceptions are intentionally excluded so retries don't
    paper over bugs.
    """

    timeout_ms: int = 10_000
    exponential_backoff: bool = False
    initial_backoff_ms: int = 1_000
    max_backoff_ms: int = 10_000
    multiplier: float = 2.0
    max_retries: int = 3
    retry_on: tuple[type[BaseException], ...] = (OSError, TimeoutError)


__all__ = ["RetryConfig", "StepMetadata"]
