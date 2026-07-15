# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Retry policy modelled on Azure ``corehttp``'s ``RetryPolicy``.

Drives the request through ``self.next`` repeatedly until the response
succeeds, the retry budget is exhausted, or a non-retryable error is
raised. State is kept in ``ctx.data["retry_settings"]`` for the duration of
one ``send`` call; per-attempt history lands in ``ctx.data["retry_history"]``.

The re-send safety gate has two axes and applies uniformly to both retryable
HTTP statuses and transport-level errors: a body-less request is re-sent based
on method idempotency, while a request carrying a body is re-sent only when that
body is replayable (see ``_request_is_resendable``). A non-replayable body is
never re-emitted, regardless of method.

A caller may pass a ``CancellationToken`` as the ``cancellation`` per-call
option: it is observed at every attempt boundary and drives the backoff wait
through an interruptible sleep, so a cooperative cancel aborts a pending backoff
promptly with ``RequestCancelledError`` rather than running the full delay. When
the budget is exhausted on the error path the surfaced exception carries a
prior-attempt trail (``add_note`` lines plus an ``attempts`` count) that excludes
the exception instance itself.

Single-use request bodies (``RequestBody.from_stream`` /
``RequestBody.from_iter``) are auto-buffered at the top of ``send`` when
the *effective* per-call retry total is positive: the policy calls
``body.to_replayable()`` and swaps the result onto the request before the
first attempt, so a retry can re-emit the same payload without raising
``RuntimeError``. The decision reads the effective total after merging
per-call overrides (``retry_total`` in ``ctx.options``) with the
constructor default, so a per-call ``retry_total=3`` over an instance built
with ``total_retries=0`` still buffers, and a per-call ``retry_total=0``
over a retrying instance skips the buffering and pays no memory cost.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from ...errors import (
    ClientAuthenticationError,
    HttpResponseError,
    RequestCancelledError,
    SdkError,
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from ...http.request.method import _IDEMPOTENT_METHODS
from ...util.clock import SYSTEM_CLOCK, CancellationToken, Clock
from .._pure.backoff import apply_jitter, compute_backoff
from .._pure.classifier import DEFAULT_RETRYABLE_STATUS, is_retryable, status_is_retryable
from .._pure.pacing import parse_rate_limit_reset, parse_retry_after, parse_retry_after_ms
from ..policy import Policy
from ..stage import Stage
from ._history import RequestHistory
from .redirect import resolve_http_tracer

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext

_LOGGER = logging.getLogger(__name__)

#: Default ceiling, in seconds, applied to a server-supplied ``Retry-After`` or
#: ``X-RateLimit-Reset`` delay so a buggy or hostile header cannot make the
#: client sleep for hours. One hour is generous for legitimate rate limits.
_DEFAULT_RETRY_AFTER_MAX: Final[float] = 3600.0

#: Header carrying the epoch second at which a rate-limit window resets. Sent by
#: GitHub, Stripe, Slack, and others alongside (or instead of) ``Retry-After``.
_RATE_LIMIT_RESET_HEADER: Final[str] = "X-RateLimit-Reset"

#: Header carrying a whole-millisecond retry delay. The vendor-neutral spelling.
_RETRY_AFTER_MS_HEADER: Final[str] = "retry-after-ms"

#: The Microsoft/Azure spelling of the whole-millisecond retry delay header.
_MS_RETRY_AFTER_MS_HEADER: Final[str] = "x-ms-retry-after-ms"

#: Upward jitter fraction applied to an ``X-RateLimit-Reset`` wait. The delay is
#: multiplied by a random sample in ``[1.0, 1.0 + this]`` — i.e. ``[100%, 120%]``
#: — so a client never wakes before the window resets, while a fleet that
#: observed the same reset instant spreads its retries instead of firing in
#: lockstep.
_RATE_LIMIT_RESET_JITTER: Final[float] = 0.2


@runtime_checkable
class _ResponseLike(Protocol):
    """Structural shape needed by the retry decision helpers.

    Both ``Response`` and ``AsyncResponse`` satisfy this implicitly via their
    ``status`` and ``headers`` attributes.
    """

    @property
    def status(self) -> Any: ...

    @property
    def headers(self) -> Any: ...


# The default retry method allow-list is *derived* from the single idempotent
# classification (`_IDEMPOTENT_METHODS`) rather than hand-maintained here, so
# the allow-list and the inherent replay-safety gate can never diverge — only a
# GET/HEAD/OPTIONS/PUT/DELETE is retried by default (no TRACE, no POST/PATCH).
_DEFAULT_METHOD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    method.value for method in _IDEMPOTENT_METHODS
)
_POST_PATCH_STATUS_RETRIES: Final[frozenset[int]] = frozenset({500, 503, 504})


class RetryMode(StrEnum):
    """Backoff schedule for ``RetryPolicy``."""

    EXPONENTIAL = "exponential"
    FIXED = "fixed"


class RetryPolicy(Policy):
    """Retry the downstream chain on transient failures.

    Counters track total, connect, read, and status retries independently;
    each attempt decrements at most one counter. The policy short-circuits
    on ``ClientAuthenticationError`` (401/403 — credentials cannot succeed
    by retrying) and respects ``Retry-After`` response headers when
    sleeping.

    Modelled directly on Azure ``corehttp``'s ``RetryPolicy`` shape: most
    knobs are the same and the configure / increment / sleep helpers split
    out so a future async twin can share the base logic.

    Attributes:
        total_retries: Hard cap on retry attempts. ``0`` disables retry.
        connect_retries: Sub-cap for connection-side errors.
        read_retries: Sub-cap for response-side errors.
        status_retries: Sub-cap for retryable HTTP status codes.
        backoff_factor: Exponential backoff base in seconds.
        backoff_max: Upper bound on a single sleep, in seconds.
        retry_mode: ``EXPONENTIAL`` or ``FIXED`` (the latter sleeps
            ``backoff_factor`` between every attempt).
        timeout: Absolute time budget for the entire chain, in seconds.
        method_allowlist: HTTP methods that get full retry semantics on any
            code in ``retry_on_status_codes``. POST and PATCH are special-cased
            and unconditionally limited to status retries on 500/503/504 — they
            cannot be widened by adding them to this allowlist.
        retry_on_status_codes: Status codes that trigger a retry.
        respect_retry_after: When ``True``, sleep for the server-supplied
            delay (``Retry-After`` header in seconds/HTTP-date, or an
            ``X-RateLimit-Reset`` epoch) when present, instead of the
            computed backoff. The server delay is itself capped at
            ``retry_after_max`` and an ``X-RateLimit-Reset`` wait gets a small
            upward jitter so a fleet of clients does not all retry at the exact
            reset instant (and never wakes before it).
        retry_after_max: Ceiling, in seconds, on a server-supplied
            ``Retry-After`` / ``X-RateLimit-Reset`` delay. Protects against a
            buggy or hostile header forcing a multi-hour sleep. Defaults to
            one hour.
        full_jitter: When ``True`` (the default), exponential backoff uses
            *full jitter* — the computed delay is multiplied by a random
            sample in ``[0.5, 1.0]`` (AWS's recommended scheme), spreading
            retries evenly across the window. When ``False``, the symmetric
            ``jitter`` band is applied instead.
        jitter: Symmetric fractional band applied to the computed backoff
            when ``full_jitter`` is ``False``. ``0.25`` multiplies the
            backoff by a random sample in ``[0.75, 1.25]``. Set to ``0`` for
            deterministic backoff.

    Example:
        ```python
        RetryPolicy(
            total_retries=3,
            backoff_factor=0.5,
            backoff_max=30,
            retry_on_status_codes={429, 500, 502, 503, 504},
        )
        ```
    """

    STAGE = Stage.RETRY

    __slots__ = (
        "_clock",
        "_full_jitter",
        "_jitter",
        "_rand",
        "backoff_factor",
        "backoff_max",
        "connect_retries",
        "method_allowlist",
        "read_retries",
        "respect_retry_after",
        "retry_after_max",
        "retry_mode",
        "retry_on_status_codes",
        "status_retries",
        "timeout",
        "total_retries",
    )

    def __init__(
        self,
        *,
        total_retries: int = 10,
        connect_retries: int = 3,
        read_retries: int = 3,
        status_retries: int = 3,
        backoff_factor: float = 0.8,
        backoff_max: float = 120.0,
        retry_mode: RetryMode = RetryMode.EXPONENTIAL,
        timeout: float = 604_800,  # 7 days, mirroring Azure's default
        method_allowlist: Iterable[str] = _DEFAULT_METHOD_ALLOWLIST,
        retry_on_status_codes: Iterable[int] = DEFAULT_RETRYABLE_STATUS,
        respect_retry_after: bool = True,
        retry_after_max: float = _DEFAULT_RETRY_AFTER_MAX,
        full_jitter: bool = True,
        jitter: float = 0.25,
        clock: Clock = SYSTEM_CLOCK,
        rand: random.Random | None = None,
    ) -> None:
        self.total_retries = total_retries
        self.connect_retries = connect_retries
        self.read_retries = read_retries
        self.status_retries = status_retries
        self.backoff_factor = backoff_factor
        self.backoff_max = backoff_max
        self.retry_mode = retry_mode
        self.timeout = timeout
        self.method_allowlist = frozenset(m.upper() for m in method_allowlist)
        self.retry_on_status_codes = frozenset(retry_on_status_codes)
        self.respect_retry_after = respect_retry_after
        self.retry_after_max = retry_after_max
        self._full_jitter = full_jitter
        self._jitter = jitter
        self._clock = clock
        self._rand = rand if rand is not None else random.Random()
        self._validate()

    def _validate(self) -> None:
        """Reject invalid scalar configuration at construction time.

        Retry counts must be non-negative integers (``0`` disables that axis),
        every duration must be finite and non-negative, and the symmetric
        jitter fraction must lie in ``[0.0, 1.0]``. Collection-valued settings
        are already defensively copied into ``frozenset`` above, so a later
        caller mutation of the source cannot alter behaviour.

        Raises:
            ValueError: When any scalar knob is out of range.
        """
        for name in ("total_retries", "connect_retries", "read_retries", "status_retries"):
            _require_non_negative_int(name, getattr(self, name))
        for name in ("backoff_factor", "backoff_max", "timeout", "retry_after_max"):
            _require_non_negative_duration(name, getattr(self, name))
        if not 0.0 <= self._jitter <= 1.0:
            raise ValueError(f"jitter must lie in [0.0, 1.0], got {self._jitter}")

    # ----- public sentinel ------------------------------------------------

    @classmethod
    def no_retries(cls) -> RetryPolicy:
        """Build a policy that never retries (``total_retries=0``)."""
        return cls(total_retries=0)

    # ----- main loop ------------------------------------------------------

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        settings = self._configure_settings(ctx.options)
        cancellation: CancellationToken | None = ctx.options.get("cancellation")
        if settings["total"] > 0 and request.body is not None and not request.body.is_replayable():
            request = request.with_body(request.body.to_replayable())
        absolute_deadline = self._clock.monotonic() + settings["timeout"]
        history: list[RequestHistory[Response]] = settings["history"]
        tracer = resolve_http_tracer(ctx)
        while True:
            _raise_if_cancelled(cancellation)
            tracer.attempt_started(len(history))
            try:
                response = self.next.send(request, ctx)
            except ClientAuthenticationError:
                raise
            except SdkError as err:
                history.append(RequestHistory(request=request, error=err))
                if not self._decrement_for_error(settings, request, err):
                    tracer.attempt_retries_exhausted()
                    ctx.data["retry_history"] = tuple(history)
                    _annotate_terminal_failure(err, history)
                    raise
                ctx.data["retry_count"] = len(history)
                delay = self._delay_for(settings, None)
                tracer.attempt_failed(err, delay)
                # Sleep outside the try so a deadline crossed during the
                # backoff raises ``ServiceResponseTimeoutError`` straight to
                # the caller instead of being swallowed by ``except SdkError``.
                self._sleep_bounded(delay, absolute_deadline, cancellation)
                _LOGGER.debug("retrying after %s: %s", type(err).__name__, err)
                continue
            if not self._is_retry(settings, request, response):
                ctx.data["retry_history"] = tuple(history)
                return response
            history.append(RequestHistory(request=request, response=response))
            if not self._decrement_status(settings):
                tracer.attempt_retries_exhausted()
                ctx.data["retry_history"] = tuple(history)
                return response
            ctx.data["retry_count"] = len(history)
            status = int(response.status)
            # Close the intermediate response before the backoff, in a
            # ``finally`` so it closes even if the delay computation throws.
            try:
                delay = self._delay_for(settings, response)
            finally:
                response.close()
            tracer.attempt_failed(_StatusRetryError(status), delay)
            self._sleep_bounded(delay, absolute_deadline, cancellation)

    # ----- configuration --------------------------------------------------

    def _configure_settings(self, options: dict[str, Any]) -> dict[str, Any]:
        """Read per-call overrides out of ``options`` into a settings dict.

        Uses non-destructive ``get`` so other policies that inspect
        ``ctx.options`` after the retry policy still see the original values.
        """
        return {
            "total": options.get("retry_total", self.total_retries),
            "connect": options.get("retry_connect", self.connect_retries),
            "read": options.get("retry_read", self.read_retries),
            "status": options.get("retry_status", self.status_retries),
            "backoff": options.get("retry_backoff_factor", self.backoff_factor),
            "max_backoff": options.get("retry_backoff_max", self.backoff_max),
            "timeout": options.get("timeout", self.timeout),
            "history": [],
        }

    # ----- retry decision -------------------------------------------------

    def _is_retry(
        self,
        settings: dict[str, Any],
        request: Request,
        response: _ResponseLike,
    ) -> bool:
        status = int(response.status)
        if status < 400:
            return False
        if not _request_is_resendable(request):
            return False
        if not self._method_is_retryable(settings, request, response):
            return False
        if status_is_retryable(status, self.retry_on_status_codes):
            return int(settings["total"]) > 0
        # ``Retry-After`` only triggers retry for status codes already in the
        # allowlist; a malicious server cannot force retries on arbitrary
        # responses by simply attaching the header.
        return False

    def _method_is_retryable(
        self,
        settings: dict[str, Any],
        request: Request,
        response: _ResponseLike | None,
    ) -> bool:
        method = str(request.method).upper()
        if response is not None and method in {"POST", "PATCH"}:
            return int(response.status) in _POST_PATCH_STATUS_RETRIES
        return method in self.method_allowlist

    def _decrement_status(self, settings: dict[str, Any]) -> bool:
        """Decrement counters after a retryable status response.

        Returns:
            ``True`` if the budget allows another attempt.
        """
        settings["total"] -= 1
        settings["status"] -= 1
        return _has_budget(settings)

    def _decrement_for_error(
        self,
        settings: dict[str, Any],
        request: Request,
        error: BaseException,
    ) -> bool:
        """Decrement counters after a network-side error.

        Retry *safety* is decided first, independently of retryability, and
        applied UNIFORMLY: a non-replayable body is never re-sent, and a
        body-less request is re-sent only when its method is idempotent — so a
        bare POST is never retried on a transport error, even a connect-phase
        one that never reached the server. Retry *eligibility* is decided
        second: ``ServiceRequestError`` and ``ServiceResponseError`` are the
        known transport families (charged to the connect / read sub-budgets);
        any other error is admitted only through the open retryability
        capability (``is_retryable``), charged to the connect sub-budget. A
        status-carrying ``HttpResponseError`` is excluded from that capability
        path — its eligibility is the configured status set, not its baked flag.

        Args:
            settings: Mutable per-call settings dict.
            request: The request that triggered the error, used to gate the
                retry on the method's idempotency and the body's replayability.
            error: The error raised by the downstream chain.

        Returns:
            ``True`` if the budget allows another attempt.
        """
        if not _request_is_resendable(request):
            return False
        if not self._method_is_retryable(settings, request, None):
            return False
        if isinstance(error, ServiceRequestError):
            settings["total"] -= 1
            settings["connect"] -= 1
        elif isinstance(error, ServiceResponseError):
            settings["total"] -= 1
            settings["read"] -= 1
        elif not isinstance(error, HttpResponseError) and is_retryable(error):
            settings["total"] -= 1
            settings["connect"] -= 1
        else:
            return False
        return _has_budget(settings)

    # ----- backoff / sleep ------------------------------------------------

    def _delay_for(
        self,
        settings: dict[str, Any],
        response: _ResponseLike | None,
    ) -> float:
        """Compute the delay before the next attempt, in seconds.

        Prefers a server-supplied signal (``Retry-After`` or
        ``X-RateLimit-Reset``) when ``respect_retry_after`` is set and the
        response carries one — capped at ``retry_after_max``. Otherwise falls
        back to the jittered computed backoff.

        Args:
            settings: Mutable per-call settings dict.
            response: The response that triggered the retry, or ``None`` for a
                network-side error (which carries no server timing header).

        Returns:
            Non-negative seconds to wait.
        """
        if response is not None and self.respect_retry_after:
            server_delay = self._server_delay(response)
            if server_delay is not None:
                return server_delay
        return self._backoff_seconds(settings)

    def _server_delay(self, response: _ResponseLike) -> float | None:
        """Resolve a server-supplied retry delay, capped and jittered.

        Applies the fixed pacing precedence (first usable wins): ``Retry-After``
        (delta-seconds, then HTTP-date), then the ``retry-after-ms`` and
        ``x-ms-retry-after-ms`` millisecond-delta headers, then an
        ``X-RateLimit-Reset`` epoch. The reset fallback gets a slight *upward*
        jitter; the jitter only ever lengthens the wait — retrying before the
        window actually resets just earns another rate-limit response — while
        the small positive spread keeps a fleet of clients that observed the
        same reset instant from retrying in lockstep. Every resolved delay is
        capped at ``retry_after_max``.

        Returns:
            Seconds to wait, or ``None`` when no recognised header is present.
        """
        headers = response.headers
        retry_after = parse_retry_after(headers.get("Retry-After"), self._clock.now())
        if retry_after is not None:
            return min(retry_after, self.retry_after_max)
        for header in (_RETRY_AFTER_MS_HEADER, _MS_RETRY_AFTER_MS_HEADER):
            delay_ms = parse_retry_after_ms(headers.get(header))
            if delay_ms is not None:
                return min(delay_ms, self.retry_after_max)
        return self._rate_limit_reset_delay(headers)

    def _rate_limit_reset_delay(self, headers: Any) -> float | None:
        """Resolve the jittered, capped ``X-RateLimit-Reset`` fallback delay.

        Args:
            headers: The response headers to read ``X-RateLimit-Reset`` from.

        Returns:
            Seconds to wait, or ``None`` when the header is absent or malformed.
        """
        reset = parse_rate_limit_reset(headers.get(_RATE_LIMIT_RESET_HEADER), self._clock.now())
        if reset is None:
            return None
        jittered = reset * self._rand.uniform(1.0, 1.0 + _RATE_LIMIT_RESET_JITTER)
        return min(jittered, self.retry_after_max)

    def _backoff_seconds(self, settings: dict[str, Any]) -> float:
        """Compute the jittered backoff delay for the next attempt, in seconds.

        Delegates the schedule to the shared pure ``compute_backoff`` (the
        deterministic exponential/fixed curve) and ``apply_jitter`` so the sync
        and async policies share one formula instead of each restating it.

        Args:
            settings: Mutable per-call settings dict; reads ``history`` (for
                the attempt count), ``backoff``, and ``max_backoff``.

        Returns:
            Non-negative seconds to wait before the next attempt.
        """
        delay = compute_backoff(
            len(settings["history"]),
            base=float(settings["backoff"]),
            cap=float(settings["max_backoff"]),
            fixed=self.retry_mode is RetryMode.FIXED,
        )
        return apply_jitter(
            delay, self._rand, full_jitter=self._full_jitter, symmetric=self._jitter
        )

    def _sleep_bounded(
        self,
        duration: float,
        absolute_deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Sleep for ``duration`` seconds, clamped to the absolute deadline.

        Both the pre-sleep ("deadline already in the past") and post-sleep
        ("deadline reached while sleeping") cases raise
        ``ServiceResponseTimeoutError``. The distinction is not meaningful to
        callers — in both cases the retry budget is exhausted — and
        ``ServiceResponseTimeoutError`` is the more accurate label for a
        budget-exhausted condition (the chain ran out of time waiting for a
        response, not establishing the request).

        When a ``CancellationToken`` is supplied the wait runs through its
        interruptible ``sleep`` (a ``threading.Event.wait``), so a cooperative
        cancel from another thread aborts the backoff promptly with
        ``RequestCancelledError`` instead of running the full delay. Cancellation
        is discriminated by exception type, never by message text.

        Args:
            duration: Desired sleep length in seconds. Non-positive values
                return immediately.
            absolute_deadline: ``Clock.monotonic()`` value beyond which the
                retry budget is considered spent.
            cancellation: Optional cooperative-cancellation handle. When present
                and already cancelled, or cancelled during the wait, the backoff
                aborts with ``RequestCancelledError``.

        Raises:
            RequestCancelledError: When ``cancellation`` is cancelled before or
                during the wait.
            ServiceResponseTimeoutError: When ``absolute_deadline`` is reached
                before or during the sleep.
        """
        _raise_if_cancelled(cancellation)
        if duration <= 0:
            return
        remaining = absolute_deadline - self._clock.monotonic()
        if remaining <= 0:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")
        actual = min(duration, remaining)
        if cancellation is None:
            self._clock.sleep(actual)
        elif not cancellation.sleep(actual):
            raise RequestCancelledError("Retry backoff aborted by cooperative cancellation")
        if self._clock.monotonic() >= absolute_deadline:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")


class _StatusRetryError(Exception):
    """Marker error passed to ``HttpTracer.attempt_failed`` for status retries.

    A retryable HTTP status response is not itself an exception, but the
    tracer's ``attempt_failed`` callback wants a ``BaseException`` describing
    why the attempt failed. This lightweight wrapper carries the status code so
    consumers can distinguish a status-driven retry from a transport error.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"retryable HTTP status {status}")
        self.status = status


def _require_non_negative_int(name: str, value: int) -> None:
    """Raise ``ValueError`` unless ``value`` is a non-negative retry count.

    Args:
        name: The configuration knob's name, for the error message.
        value: The candidate retry count.

    Raises:
        ValueError: When ``value`` is negative.
    """
    if value < 0:
        raise ValueError(f"{name} must be >= 0 (0 disables that axis), got {value}")


def _require_non_negative_duration(name: str, value: float) -> None:
    """Raise ``ValueError`` unless ``value`` is a finite, non-negative duration.

    Args:
        name: The configuration knob's name, for the error message.
        value: The candidate duration, in seconds.

    Raises:
        ValueError: When ``value`` is NaN, infinite, or negative.
    """
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative duration in seconds, got {value}")


def _has_budget(settings: dict[str, Any]) -> bool:
    """Return ``True`` while every retry counter is non-negative."""
    counts: tuple[int, ...] = (
        settings["total"],
        settings["connect"],
        settings["read"],
        settings["status"],
    )
    return min(counts) >= 0


def _request_is_resendable(request: Request) -> bool:
    """Return whether ``request`` can be safely re-sent on a retry.

    The body axis of the two-axis re-send safety gate: a body-less request is
    always re-sendable (idempotency is decided separately by method), while a
    request carrying a body may only be re-sent when that body is replayable.
    A non-replayable body — one the caller declined or is unable to buffer — is
    never re-emitted, regardless of the method or which failure triggered the
    retry.

    Args:
        request: The request whose body is inspected.

    Returns:
        ``True`` when the request has no body or a replayable one.
    """
    body = request.body
    return body is None or body.is_replayable()


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    """Raise ``RequestCancelledError`` when a cooperative token is cancelled.

    A no-op when ``cancellation`` is ``None`` or still live, so the retry loop
    can call it unconditionally at attempt boundaries.

    Args:
        cancellation: The cooperative-cancellation handle, if any.

    Raises:
        RequestCancelledError: When the token has been cancelled.
    """
    if cancellation is not None and cancellation.cancelled:
        raise RequestCancelledError("Retry cancelled by cooperative cancellation")


def _annotate_terminal_failure[ResponseT](
    error: BaseException,
    history: Sequence[RequestHistory[ResponseT]],
) -> None:
    """Attach the prior-attempt trail to a terminal (retries-exhausted) failure.

    Records the total attempt count on an ``attempts`` attribute and appends one
    ``add_note`` line per PRIOR attempt. The currently-surfacing exception is the
    last history entry; it is excluded from its own trail so an exception never
    notes itself.

    Args:
        error: The exception about to surface to the caller.
        history: The full per-attempt trail, ending with ``error``'s own entry.
    """
    # Stamp the count as a dynamic instance attribute — the SDK exception types
    # carry no static ``attempts`` field, so this writes straight into __dict__.
    error.__dict__["attempts"] = len(history)
    prior = [entry for entry in history if entry.error is not error]
    for index, entry in enumerate(prior, start=1):
        error.add_note(f"prior attempt {index}: {_describe_attempt(entry)}")


def _describe_attempt[ResponseT](entry: RequestHistory[ResponseT]) -> str:
    """Render a one-line summary of a single retry attempt for the trail.

    Args:
        entry: The per-attempt history record.

    Returns:
        A short human-readable description of how the attempt ended.
    """
    if entry.error is not None:
        return f"raised {type(entry.error).__name__}: {entry.error}"
    status = getattr(entry.response, "status", None)
    if status is not None:
        return f"returned HTTP {int(status)}"
    return "produced no response"


__all__ = ["RetryMode", "RetryPolicy"]
