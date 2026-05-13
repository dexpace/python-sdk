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
from collections.abc import Callable, Iterable
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from ...errors import (
    ClientAuthenticationError,
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from ..policy import Policy
from ._history import RequestHistory

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext

_LOGGER = logging.getLogger(__name__)


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
        respect_retry_after: When ``True``, sleep for the ``Retry-After``
            header value (if present) instead of the computed backoff.
        jitter: Fractional band applied to each computed backoff to break
            thundering herds. ``0.25`` multiplies the backoff by a random
            sample in ``[0.75, 1.25]``. Set to ``0`` for deterministic
            backoff.

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
        jitter: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
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
        self._jitter = jitter
        self._sleep = sleep
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
        absolute_deadline = time.monotonic() + settings["timeout"]
        history: list[RequestHistory[Response]] = settings["history"]
        while True:
            try:
                response = self.next.send(request, ctx)
                if not self._is_retry(settings, request, response):
                    ctx.data["retry_history"] = tuple(history)
                    return response
                history.append(RequestHistory(request=request, response=response))
                if not self._decrement_status(settings):
                    ctx.data["retry_history"] = tuple(history)
                    return response
                ctx.data["retry_count"] = len(history)
                self._sleep_for(settings, response, absolute_deadline)
                continue
            except ClientAuthenticationError:
                raise
            except SdkError as err:
                history.append(RequestHistory(request=request, error=err))
                if not self._decrement_for_error(settings, err):
                    ctx.data["retry_history"] = tuple(history)
                    raise
                ctx.data["retry_count"] = len(history)
                self._sleep_for(settings, None, absolute_deadline)
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

    def _sleep_for(
        self,
        settings: dict[str, Any],
        response: _ResponseLike | None,
        absolute_deadline: float,
    ) -> None:
        """Sleep before the next attempt, respecting the absolute deadline."""
        if response is not None and self.respect_retry_after:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                self._sleep_bounded(retry_after, absolute_deadline)
                return
        self._sleep_bounded(self._backoff_seconds(settings), absolute_deadline)

    def _backoff_seconds(self, settings: dict[str, Any]) -> float:
        attempts = len(settings["history"])
        if attempts <= 1:
            return 0.0
        if self.retry_mode is RetryMode.FIXED:
            backoff = float(settings["backoff"])
        else:
            backoff = float(settings["backoff"]) * (2 ** (attempts - 1))
        bounded = min(float(settings["max_backoff"]), backoff)
        if self._jitter == 0:
            return bounded
        return bounded * self._rand.uniform(1 - self._jitter, 1 + self._jitter)

    def _sleep_bounded(self, duration: float, absolute_deadline: float) -> None:
        if duration <= 0:
            return
        remaining = absolute_deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceRequestTimeoutError("Retry budget exhausted (timeout reached)")
        actual = min(duration, remaining)
        self._sleep(actual)
        if time.monotonic() >= absolute_deadline:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")


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
