# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that opens a span around the downstream chain.

Besides the OpenTelemetry span, the policy drives two correlation seams:

- It mints a per-operation ``HttpTracer`` from
  ``ctx.call.instrumentation_context.http_tracer_factory`` and emits the
  fine-grained operation/request/response lifecycle events an SRE wants
  (``operation_started``, ``request_sent``, ``response_headers_received``,
  ``response_received``, ``operation_succeeded`` / ``operation_failed``). Per
  attempt events (``attempt_started`` / ``attempt_failed`` /
  ``attempt_retries_exhausted``) are owned by the retry policy.
- It binds the active trace/span ids into ``contextvars`` for the duration of
  the request so ``ClientLogger`` can stamp ``trace.id`` / ``span.id`` onto
  every log record emitted downstream.

Both seams are no-op-safe: the default tracer factory returns
``NOOP_HTTP_TRACER`` and the no-op span carries the sentinel trace ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...instrumentation import NOOP_TRACER, Tracer, bind_correlation
from ..policy import Policy
from ..stage import Stage
from .redirect import resolve_http_tracer

if TYPE_CHECKING:
    from ...http.common.url import Url
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ...instrumentation import HttpTracer, Span
    from ..context import PipelineContext


class TracingPolicy(Policy):
    """Wrap each request in a tracing span.

    Span attributes follow OpenTelemetry semantic conventions:

    - ``http.request.method``: HTTP method.
    - ``url.full``: Full URL (no redaction — install a separate redactor
      if you need it).
    - ``server.address`` / ``server.port``: Resolved from the URL.
    - ``http.response.status_code``: Set on success.
    - ``error.type``: Set on exception.
    - ``http.request.resend_count``: Retry attempt count from
      ``ctx.data["retry_count"]`` (when retry policy is upstream).

    While the span is open the active trace/span ids are bound into the
    correlation ``contextvars`` so downstream log records carry them, and a
    per-operation ``HttpTracer`` (from the call's
    ``instrumentation_context.http_tracer_factory``) receives the
    operation/request/response lifecycle events.

    Disable per-call by setting ``ctx.options["tracing_enabled"] = False``.
    """

    STAGE = Stage.POST_LOGGING
    __slots__ = ("_tracer",)

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tracer = tracer or NOOP_TRACER

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if not ctx.options.get("tracing_enabled", True):
            return self.next.send(request, ctx)
        parent = ctx.call.instrumentation_context
        # Share one per-operation tracer with the redirect / retry policies via
        # ``ctx.data`` (whichever policy runs first mints it).
        http_tracer = resolve_http_tracer(ctx)
        span = self._tracer.start_span(f"HTTP {request.method}", parent=parent)
        _set_request_attributes(span, request)
        http_tracer.operation_started()
        with bind_correlation(trace_id=_trace_id(span), span_id=_span_id(span)):
            return self._dispatch(request, ctx, span, http_tracer)

    def _dispatch(
        self,
        request: Request,
        ctx: PipelineContext,
        span: Span,
        http_tracer: HttpTracer,
    ) -> Response:
        """Run the downstream chain, emitting tracer events around it."""
        _notify_request_sent(http_tracer, request)
        try:
            with span.make_current():
                response = self.next.send(request, ctx)
        except BaseException as err:
            span.set_error(type(err).__name__)
            span.end(error=err)
            http_tracer.operation_failed(err)
            raise
        _notify_response(http_tracer, response)
        span.set_attribute("http.response.status_code", int(response.status))
        retry_count = ctx.data.get("retry_count")
        if isinstance(retry_count, int) and retry_count > 0:
            span.set_attribute("http.request.resend_count", retry_count)
        span.end()
        http_tracer.operation_succeeded()
        return response


def _set_request_attributes(span: Span, request: Request) -> None:
    """Stamp the OpenTelemetry request attributes onto the span."""
    host, port = _split_host(request.url)
    span.set_attribute("http.request.method", str(request.method))
    span.set_attribute("url.full", str(request.url))
    if host:
        span.set_attribute("server.address", host)
    if port is not None:
        span.set_attribute("server.port", port)


def _notify_request_sent(http_tracer: HttpTracer, request: Request) -> None:
    """Emit ``request_sent`` with the known body byte count, if any."""
    body = request.body
    if body is None:
        http_tracer.request_sent(0)
        return
    length = body.content_length()
    if length >= 0:
        http_tracer.request_sent(length)


def _notify_response(http_tracer: HttpTracer, response: Response) -> None:
    """Emit ``response_headers_received`` then ``response_received``."""
    headers = {name: ", ".join(values) for name, values in response.headers.items()}
    http_tracer.response_headers_received(int(response.status), headers)
    body = response.body
    if body is None:
        http_tracer.response_received(0)
        return
    length = body.content_length()
    if length >= 0:
        http_tracer.response_received(length)


def _trace_id(span: Span) -> str | None:
    value = span.context.trace_id.value
    return value if span.context.is_valid else None


def _span_id(span: Span) -> str | None:
    return span.context.span_id.value if span.context.is_valid else None


def _split_host(url: Url) -> tuple[str | None, int | None]:
    return url.host or None, url.port


__all__ = ["TracingPolicy"]
