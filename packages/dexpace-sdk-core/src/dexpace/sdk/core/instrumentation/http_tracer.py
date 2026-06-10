# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Fine-grained HTTP request telemetry callbacks.

``HttpTracer`` is an event sink that pipeline policies notify at the moments
that matter to an operator: each attempt boundary, retry exhaustion, byte
counts on the wire, and connection acquisition. Every method is a no-op by
default, so a consumer overrides only the events their backend cares about and
inherits the rest. ``HttpTracerFactory`` mints a fresh tracer per logical
operation so per-call state (attempt counters, timers) stays isolated.

Modeled on Google gax's ``ApiTracer``. The SDK ships the contract plus a no-op
default (`NoopHttpTracer` / `NOOP_HTTP_TRACER_FACTORY`); consumers
plug in a real implementation per their metrics/tracing stack.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping


class HttpTracer(ABC):
    """Sink for fine-grained HTTP request events.

    One instance tracks a single logical operation across its (possibly
    retried) attempts. Pipeline policies call these methods at the appropriate
    moments; the default implementations do nothing, so subclasses override
    only the events they consume.

    Implementations are notified from whatever thread or task drives the
    request and should keep callbacks cheap and non-blocking; they must not
    raise. The SDK does not guard these callbacks: a raising tracer propagates
    out of the notifying policy and can mask the original error (for example,
    inside ``TracingPolicy._dispatch``), so the no-raise rule is a hard
    requirement on the implementation, not something the SDK enforces.
    """

    def operation_started(self) -> None:
        """The overall operation began (before the first attempt)."""

    def operation_succeeded(self) -> None:
        """The operation completed successfully."""

    def operation_failed(self, error: BaseException) -> None:
        """The operation failed permanently.

        Args:
            error: The exception that terminated the operation.
        """

    def attempt_started(self, attempt: int) -> None:
        """A new attempt began.

        Args:
            attempt: Zero-based index of the attempt about to be sent.
        """

    def attempt_failed(self, error: BaseException, next_delay: float) -> None:
        """An attempt failed and a retry is scheduled.

        Args:
            error: The exception that failed the attempt.
            next_delay: Seconds the policy will wait before the next attempt.
        """

    def attempt_retries_exhausted(self) -> None:
        """The retry budget was exhausted; no further attempts will be made."""

    def request_url_resolved(self, url: str) -> None:
        """The final request URL was resolved (post-redirect).

        Args:
            url: The absolute URL the attempt targets.
        """

    def request_sent(self, byte_count: int) -> None:
        """The request body finished writing to the wire.

        Args:
            byte_count: Number of body bytes written.
        """

    def response_headers_received(self, status: int, headers: Mapping[str, str]) -> None:
        """Response status and headers arrived (before the body).

        Args:
            status: HTTP status code of the response.
            headers: The response headers.
        """

    def response_received(self, byte_count: int) -> None:
        """The response body finished reading from the wire.

        Args:
            byte_count: Number of body bytes read.
        """

    def connection_acquired(self, host: str, port: int) -> None:
        """A transport connection was acquired for the attempt.

        Args:
            host: Remote host the connection targets.
            port: Remote port the connection targets.
        """


@runtime_checkable
class HttpTracerFactory(Protocol):
    """Mints a fresh `HttpTracer` per logical operation.

    Policies create one tracer at the start of each operation so per-call state
    (attempt counters, timers) never leaks across operations.
    """

    def create(self) -> HttpTracer:
        """Return a new tracer for one operation."""
        ...


__all__ = ["HttpTracer", "HttpTracerFactory"]
