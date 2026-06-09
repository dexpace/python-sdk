# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Retry policy modelled on Azure ``corehttp``'s ``RetryPolicy``.

Drives the request through ``self.next`` repeatedly until the response
succeeds, the retry budget is exhausted, or a non-retryable error is
raised. State is kept in ``ctx.data["retry_settings"]`` for the duration of
one ``send`` call; per-attempt history lands in ``ctx.data["retry_history"]``.

Single-use request bodies (``RequestBody.from_stream`` /
``RequestBody.from_iter``) are auto-buffered at the top of ``send`` when
``total_retries > 0``: the policy calls ``body.to_replayable()`` and swaps
the result onto the request before the first attempt, so a retry can
re-emit the same payload without raising ``RuntimeError``. The buffering
step is skipped when ``total_retries == 0`` so callers who explicitly opt
out of retries pay no memory cost.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Iterable
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from ...errors import (
    ClientAuthenticationError,
    SdkError,
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from ...util.clock import SYSTEM_CLOCK, Clock
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

#: Upward jitter fraction applied to an ``X-RateLimit-Reset`` wait. The delay is
#: multiplied by a random sample in ``[1.0, 1.0 + this]`` so a client never wakes
#: before the window resets, while a fleet that observed the same reset instant
#: spreads its retries instead of firing in lockstep.
_RATE_LIMIT_RESET_JITTER: Final[float] = 0.1


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


_DEFAULT_STATUS_RETRIES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
_DEFAULT_METHOD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"}
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
        method_allowlist: HTTP methods that get full retry semantics.
            POST and PATCH are retried only on 500/503/504.
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
        retry_on_status_codes: Iterable[int] = _DEFAULT_STATUS_RETRIES,
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

    # ----- public sentinel ------------------------------------------------

    @classmethod
    def no_retries(cls) -> RetryPolicy:
        """Build a policy that never retries (``total_retries=0``)."""
        return cls(total_retries=0)

    # ----- main loop ------------------------------------------------------

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if self.total_retries > 0 and request.body is not None and not request.body.is_replayable():
            request = request.with_body(request.body.to_replayable())
        settings = self._configure_settings(ctx.options)
        absolute_deadline = self._clock.monotonic() + settings["timeout"]
        history: list[RequestHistory[Response]] = settings["history"]
        tracer = resolve_http_tracer(ctx)
        while True:
            tracer.attempt_started(len(history))
            try:
                response = self.next.send(request, ctx)
                if not self._is_retry(settings, request, response):
                    ctx.data["retry_history"] = tuple(history)
                    return response
                history.append(RequestHistory(request=request, response=response))
                if not self._decrement_status(settings):
                    tracer.attempt_retries_exhausted()
                    ctx.data["retry_history"] = tuple(history)
                    return response
                ctx.data["retry_count"] = len(history)
                delay = self._delay_for(settings, response)
                tracer.attempt_failed(_StatusRetryError(int(response.status)), delay)
                self._sleep_bounded(delay, absolute_deadline)
                continue
            except ClientAuthenticationError:
                raise
            except SdkError as err:
                history.append(RequestHistory(request=request, error=err))
                if not self._decrement_for_error(settings, err):
                    tracer.attempt_retries_exhausted()
                    ctx.data["retry_history"] = tuple(history)
                    raise
                ctx.data["retry_count"] = len(history)
                delay = self._delay_for(settings, None)
                tracer.attempt_failed(err, delay)
                self._sleep_bounded(delay, absolute_deadline)
                _LOGGER.debug("retrying after %s: %s", type(err).__name__, err)
                continue

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
        if not self._method_is_retryable(settings, request, response):
            return False
        if status in self.retry_on_status_codes:
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
        error: BaseException,
    ) -> bool:
        """Decrement counters after a network-side error.

        Returns:
            ``True`` if the budget allows another attempt.
        """
        settings["total"] -= 1
        if isinstance(error, ServiceRequestError):
            settings["connect"] -= 1
        elif isinstance(error, ServiceResponseError):
            settings["read"] -= 1
        else:  # pragma: no cover - upstream raised something we don't classify
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

        ``Retry-After`` (seconds or HTTP-date) takes precedence; an
        ``X-RateLimit-Reset`` epoch is the fallback and gets a slight *upward*
        jitter. The jitter only ever lengthens the wait — retrying before the
        window actually resets just earns another rate-limit response — while
        the small positive spread keeps a fleet of clients that observed the
        same reset instant from retrying in lockstep. Both are capped at
        ``retry_after_max``.

        Returns:
            Seconds to wait, or ``None`` when neither header is present.
        """
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return min(retry_after, self.retry_after_max)
        reset = _parse_rate_limit_reset(
            response.headers.get(_RATE_LIMIT_RESET_HEADER),
            self._clock.now(),
        )
        if reset is None:
            return None
        jittered = reset * self._rand.uniform(1.0, 1.0 + _RATE_LIMIT_RESET_JITTER)
        return min(jittered, self.retry_after_max)

    def _backoff_seconds(self, settings: dict[str, Any]) -> float:
        attempts = len(settings["history"])
        if attempts <= 1:
            return 0.0
        if self.retry_mode is RetryMode.FIXED:
            backoff = float(settings["backoff"])
        else:
            backoff = float(settings["backoff"]) * (2 ** (attempts - 1))
        bounded = min(float(settings["max_backoff"]), backoff)
        if self._full_jitter:
            return bounded * self._rand.uniform(0.5, 1.0)
        if self._jitter == 0:
            return bounded
        return bounded * self._rand.uniform(1 - self._jitter, 1 + self._jitter)

    def _sleep_bounded(self, duration: float, absolute_deadline: float) -> None:
        """Sleep for ``duration`` seconds, clamped to the absolute deadline.

        Both the pre-sleep ("deadline already in the past") and post-sleep
        ("deadline reached while sleeping") cases raise
        ``ServiceResponseTimeoutError``. The distinction is not meaningful to
        callers — in both cases the retry budget is exhausted — and
        ``ServiceResponseTimeoutError`` is the more accurate label for a
        budget-exhausted condition (the chain ran out of time waiting for a
        response, not establishing the request).

        Args:
            duration: Desired sleep length in seconds. Non-positive values
                return immediately.
            absolute_deadline: ``Clock.monotonic()`` value beyond which the
                retry budget is considered spent.

        Raises:
            ServiceResponseTimeoutError: When ``absolute_deadline`` is reached
                before or during the sleep.
        """
        if duration <= 0:
            return
        remaining = absolute_deadline - self._clock.monotonic()
        if remaining <= 0:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")
        actual = min(duration, remaining)
        self._clock.sleep(actual)
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


_RETRY_AFTER_DELTA_PATTERN = re.compile(r"^\s*\d+(\.\d+)?\s*$")


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date).

    Args:
        value: Raw header value. ``None`` returns ``None`` directly.

    Returns:
        Seconds to wait (>= 0), or ``None`` when ``value`` is missing or
        unparseable.
    """
    if value is None or not value.strip():
        return None
    if _RETRY_AFTER_DELTA_PATTERN.match(value):
        return max(0.0, float(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    return max(0.0, delta)


_RATE_LIMIT_RESET_PATTERN = re.compile(r"^\s*\d+(\.\d+)?\s*$")


def _parse_rate_limit_reset(value: str | None, now: float) -> float | None:
    """Parse an ``X-RateLimit-Reset`` epoch header into a delay.

    The header carries the wall-clock second at which the rate-limit window
    resets (GitHub, Stripe, Slack). The delay is the difference between that
    instant and ``now``, floored at zero (a reset already in the past means
    retry immediately).

    Args:
        value: Raw header value (epoch seconds). ``None`` or unparseable
            returns ``None``.
        now: Current wall-clock time, in seconds since the epoch, used to
            compute the delta. Injected so the value is deterministic in tests.

    Returns:
        Seconds to wait (>= 0), or ``None`` when the header is missing or not a
        plain epoch number.
    """
    if value is None or not value.strip():
        return None
    if not _RATE_LIMIT_RESET_PATTERN.match(value):
        return None
    return max(0.0, float(value) - now)


def _has_budget(settings: dict[str, Any]) -> bool:
    """Return ``True`` while every retry counter is non-negative."""
    counts: tuple[int, ...] = (
        settings["total"],
        settings["connect"],
        settings["read"],
        settings["status"],
    )
    return min(counts) >= 0


__all__ = ["RetryMode", "RetryPolicy"]
