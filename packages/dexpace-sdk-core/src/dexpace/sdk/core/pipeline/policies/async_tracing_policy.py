# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twins of the tracing policies (`OperationTracingPolicy` / `TracingPolicy`)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ...instrumentation import (
    NOOP_HTTP_TRACER,
    NOOP_TRACER,
    Tracer,
    UrlRedactor,
    bind_correlation,
)
from ..async_policy import AsyncPolicy
from ..stage import Stage
from .redirect import resolve_http_tracer
from .tracing_policy import (
    _OPERATION_BRACKET_KEY,
    _notify_request_sent,
    _set_request_attributes,
    _span_id,
    _trace_id,
    _warn_missing_operation_bracket_once,
)

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ...instrumentation import HttpTracer, Span
    from ..context import PipelineContext


class AsyncOperationTracingPolicy(AsyncPolicy):
    """Async variant of `OperationTracingPolicy`.

    Emits the per-operation ``HttpTracer`` lifecycle around the whole async
    call. Placed at `Stage.OPERATION`, outside the redirect and retry wrappers,
    so its single ``send`` brackets every hop and attempt: it emits
    ``operation_started`` before dispatching the chain and exactly one of
    ``operation_succeeded`` / ``operation_failed`` once the chain unwinds, so
    the operation outcome reflects what the caller observes rather than the
    result of the first attempt.

    This completes the async ``HttpTracer`` lifecycle. `AsyncRetryPolicy` and
    `AsyncRedirectPolicy` already emit the attempt-level events and
    ``request_url_resolved`` through the same per-operation tracer (resolved
    via ``resolve_http_tracer`` and cached in ``ctx.data``), so without this
    policy the async stack reports attempts but never the operation outcome.
    The tracer callbacks are synchronous, so the body matches the sync twin
    apart from the ``await`` on the downstream send.

    Disable per-call by setting ``ctx.options["tracing_enabled"] = False``.

    Attributes:
        STAGE: Pinned to `Stage.OPERATION` at the type level so mis-slotting is
            caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.OPERATION]] = Stage.OPERATION
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Bracket the downstream chain with the per-operation lifecycle.

        Args:
            request: Outgoing request.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        if not ctx.options.get("tracing_enabled", True):
            return await self.next.send(request, ctx)
        http_tracer = resolve_http_tracer(ctx)
        http_tracer.operation_started()
        try:
            response = await self.next.send(request, ctx)
        except BaseException as err:
            http_tracer.operation_failed(err)
            raise
        http_tracer.operation_succeeded()
        return response


class AsyncTracingPolicy(AsyncPolicy):
    """Async twin of `TracingPolicy`: a per-attempt span plus per-request events.

    Re-entered once per retry attempt / redirect hop (it sits *inside* those
    wrappers), so it opens one span per attempt with the same OpenTelemetry
    semantic-convention attributes the sync `TracingPolicy` sets
    (``http.request.method``, redacted ``url.full``, ``server.address`` /
    ``server.port``, ``http.response.status_code``, ``error.type``,
    ``http.request.resend_count``). While the span is open the active
    trace/span ids are bound into the correlation ``contextvars`` — which
    asyncio propagates across ``await`` boundaries — so downstream log records
    carry them, and the per-operation ``HttpTracer`` receives the per-request
    events (``request_sent``, ``response_headers_received``,
    ``response_received``).

    Span emission and the tracer callbacks are independent of the logging
    policy: they fire whether or not `AsyncLoggingPolicy` is enabled. The
    operation-level lifecycle is emitted by `AsyncOperationTracingPolicy`; if a
    real ``HttpTracer`` is installed without that bracket, this policy logs the
    same one-time warning the sync twin does.

    Disable per-call by setting ``ctx.options["tracing_enabled"] = False``.

    Attributes:
        STAGE: Pinned to `Stage.POST_LOGGING` so it observes the trace id after
            logging, mirroring the sync policy's slot.
    """

    STAGE: ClassVar[Literal[Stage.POST_LOGGING]] = Stage.POST_LOGGING
    __slots__ = ("_redactor", "_tracer")

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        redactor: UrlRedactor | None = None,
    ) -> None:
        self._tracer = tracer or NOOP_TRACER
        self._redactor = redactor or UrlRedactor()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        if not ctx.options.get("tracing_enabled", True):
            return await self.next.send(request, ctx)
        parent = ctx.call.instrumentation_context
        http_tracer = resolve_http_tracer(ctx)
        if http_tracer is not NOOP_HTTP_TRACER and _OPERATION_BRACKET_KEY not in ctx.data:
            _warn_missing_operation_bracket_once()
        span = self._tracer.start_span(f"HTTP {request.method}", parent=parent)
        _set_request_attributes(span, request, self._redactor)
        with bind_correlation(trace_id=_trace_id(span), span_id=_span_id(span)):
            return await self._dispatch(request, ctx, span, http_tracer)

    async def _dispatch(
        self,
        request: Request,
        ctx: PipelineContext,
        span: Span,
        http_tracer: HttpTracer,
    ) -> AsyncResponse:
        """Run the downstream chain, emitting per-attempt tracer events around it."""
        _notify_request_sent(http_tracer, request)
        try:
            with span.make_current():
                response = await self.next.send(request, ctx)
        except BaseException as err:
            span.set_error(type(err).__name__)
            span.end(error=err)
            raise
        _notify_response(http_tracer, response)
        span.set_attribute("http.response.status_code", int(response.status))
        retry_count = ctx.data.get("retry_count")
        if isinstance(retry_count, int) and retry_count > 0:
            span.set_attribute("http.request.resend_count", retry_count)
        span.end()
        return response


def _notify_response(http_tracer: HttpTracer, response: AsyncResponse) -> None:
    """Emit ``response_headers_received`` then ``response_received``.

    The async twin of the sync helper of the same name: identical shape, typed
    for `AsyncResponse` (whose body exposes the same ``content_length``).
    """
    headers = {name: ", ".join(values) for name, values in response.headers.items()}
    http_tracer.response_headers_received(int(response.status), headers)
    body = response.body
    if body is None:
        http_tracer.response_received(0)
        return
    length = body.content_length()
    if length >= 0:
        http_tracer.response_received(length)


__all__ = ["AsyncOperationTracingPolicy", "AsyncTracingPolicy"]
