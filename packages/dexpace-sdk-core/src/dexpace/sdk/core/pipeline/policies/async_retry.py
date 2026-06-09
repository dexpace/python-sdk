# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``RetryPolicy``.

Shares the per-attempt classification helpers with the sync variant by
delegating into the same private methods on ``RetryPolicy``. The async
twin reimplements only the dispatch loop (using ``await``) and the sleep
helper (using an async sleep callable).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ...errors import (
    ClientAuthenticationError,
    SdkError,
    ServiceResponseTimeoutError,
)
from ...util.clock import ASYNC_SYSTEM_CLOCK, AsyncClock
from ..async_policy import AsyncPolicy
from ..stage import Stage
from ._history import RequestHistory
from .redirect import resolve_http_tracer
from .retry import RetryMode, RetryPolicy, _StatusRetryError

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext

_LOGGER = logging.getLogger(__name__)


class AsyncRetryPolicy(AsyncPolicy):
    """Async retry policy.

    Reuses ``RetryPolicy`` for configuration and per-attempt classification;
    the dispatch loop is awaited and the sleep callable is async.

    Attributes:
        config: The underlying sync ``RetryPolicy`` carrying knobs and
            classification helpers.
    """

    STAGE = Stage.RETRY

    config: RetryPolicy

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
        timeout: float = 604_800,
        method_allowlist: Iterable[str] | None = None,
        retry_on_status_codes: Iterable[int] | None = None,
        respect_retry_after: bool = True,
        retry_after_max: float | None = None,
        full_jitter: bool = True,
        jitter: float = 0.25,
        clock: AsyncClock = ASYNC_SYSTEM_CLOCK,
        rand: random.Random | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "total_retries": total_retries,
            "connect_retries": connect_retries,
            "read_retries": read_retries,
            "status_retries": status_retries,
            "backoff_factor": backoff_factor,
            "backoff_max": backoff_max,
            "retry_mode": retry_mode,
            "timeout": timeout,
            "respect_retry_after": respect_retry_after,
            "full_jitter": full_jitter,
            "jitter": jitter,
        }
        if retry_after_max is not None:
            kwargs["retry_after_max"] = retry_after_max
        if method_allowlist is not None:
            kwargs["method_allowlist"] = method_allowlist
        if retry_on_status_codes is not None:
            kwargs["retry_on_status_codes"] = retry_on_status_codes
        if rand is not None:
            kwargs["rand"] = rand
        self.config = RetryPolicy(**kwargs)
        self._clock = clock

    @classmethod
    def no_retries(cls) -> AsyncRetryPolicy:
        return cls(total_retries=0)

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        cfg = self.config
        if cfg.total_retries > 0 and request.body is not None and not request.body.is_replayable():
            request = request.with_body(request.body.to_replayable())
        settings = cfg._configure_settings(ctx.options)
        absolute_deadline = self._clock.monotonic() + settings["timeout"]
        history: list[RequestHistory[AsyncResponse]] = settings["history"]
        tracer = resolve_http_tracer(ctx)
        while True:
            tracer.attempt_started(len(history))
            try:
                response = await self.next.send(request, ctx)
            except ClientAuthenticationError:
                raise
            except asyncio.CancelledError:
                # CancelledError is a BaseException, not an SdkError, so the
                # ``except SdkError`` below would not catch it — but an explicit
                # re-raise documents and guarantees the invariant: a cancelled
                # request is never retried, it propagates immediately.
                raise
            except SdkError as err:
                history.append(RequestHistory(request=request, error=err))
                if not cfg._decrement_for_error(settings, err):
                    tracer.attempt_retries_exhausted()
                    ctx.data["retry_history"] = tuple(history)
                    raise
                ctx.data["retry_count"] = len(history)
                delay = cfg._delay_for(settings, None)
                tracer.attempt_failed(err, delay)
                await self._sleep_bounded(delay, absolute_deadline)
                _LOGGER.debug("retrying after %s: %s", type(err).__name__, err)
                continue
            if not cfg._is_retry(settings, request, response):
                ctx.data["retry_history"] = tuple(history)
                return response
            history.append(RequestHistory(request=request, response=response))
            if not cfg._decrement_status(settings):
                tracer.attempt_retries_exhausted()
                ctx.data["retry_history"] = tuple(history)
                return response
            ctx.data["retry_count"] = len(history)
            delay = cfg._delay_for(settings, response)
            tracer.attempt_failed(_StatusRetryError(int(response.status)), delay)
            await self._sleep_bounded(delay, absolute_deadline)

    async def _sleep_bounded(
        self,
        duration: float,
        absolute_deadline: float,
    ) -> None:
        """Sleep up to ``duration`` seconds, bounded by ``absolute_deadline``.

        Raises ``ServiceResponseTimeoutError`` when the retry budget is
        exhausted — both before sleeping (deadline already past) and after
        (deadline reached during sleep). The "response timeout" framing is
        the more accurate label for "budget exhausted"; the prior split
        between request- and response-timeout was a distinction without a
        difference at this boundary.
        """
        if duration <= 0:
            return
        remaining = absolute_deadline - self._clock.monotonic()
        if remaining <= 0:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")
        actual = min(duration, remaining)
        await self._clock.sleep(actual)
        if self._clock.monotonic() >= absolute_deadline:
            raise ServiceResponseTimeoutError("Retry budget exhausted (timeout reached)")


__all__ = ["AsyncRetryPolicy"]
