# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policies that drive the tracing and correlation seams.

Tracing is split across two policies that observe the request at two
different scopes:

- `OperationTracingPolicy` sits at `Stage.OPERATION`, *outside* the redirect
  and retry wrappers, so a single entry brackets the whole operation no
  matter how many hops or attempts happen inside. It emits the per-operation
  ``HttpTracer`` lifecycle events that must fire exactly once and reflect the
  final outcome: ``operation_started`` before the chain runs, then exactly one
  of ``operation_succeeded`` / ``operation_failed`` once it unwinds. A call
  that fails on its first attempt and succeeds on a retry therefore reports a
  single ``operation_succeeded`` (and a call that exhausts its retries reports
  a single ``operation_failed`` carrying the error that actually escaped).
- `TracingPolicy` sits at `Stage.POST_LOGGING`, *inside* the wrappers, so it
  is re-entered once per attempt / hop. It opens an OpenTelemetry span per
  attempt, emits the per-request ``HttpTracer`` events (``request_sent``,
  ``response_headers_received``, ``response_received``), and binds the active
  trace / span ids into ``contextvars`` so ``ClientLogger`` can stamp them onto
  every log record emitted while the span is current. Per-attempt retry events
  (``attempt_started`` / ``attempt_failed`` / ``attempt_retries_exhausted``)
  are owned by the retry policy.

Both policies resolve the same per-operation ``HttpTracer`` via
``resolve_http_tracer`` (cached in ``ctx.data``), and both are no-op-safe: the
default tracer factory returns ``NOOP_HTTP_TRACER`` and the no-op span carries
the sentinel trace ids. Disable either per-call by setting
``ctx.options["tracing_enabled"] = False``.
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


class OperationTracingPolicy(Policy):
    """Emit the per-operation ``HttpTracer`` lifecycle around the whole call.

    Placed at `Stage.OPERATION`, outside the redirect and retry wrappers, so
    its single ``send`` brackets every hop and attempt. It emits
    ``operation_started`` before dispatching the chain and exactly one of
    ``operation_succeeded`` / ``operation_failed`` once the chain unwinds, so
    the operation outcome reflects what the caller actually observes rather
    than the result of the first attempt.

    The per-operation ``HttpTracer`` is shared with `TracingPolicy` and the
    redirect / retry policies via ``resolve_http_tracer`` (cached in
    ``ctx.data``). Disable per-call by setting
    ``ctx.options["tracing_enabled"] = False``.
    """

    STAGE = Stage.OPERATION
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if not ctx.options.get("tracing_enabled", True):
            return self.next.send(request, ctx)
        http_tracer = resolve_http_tracer(ctx)
        http_tracer.operation_started()
        try:
            response = self.next.send(request, ctx)
        except BaseException as err:
            http_tracer.operation_failed(err)
            raise
        http_tracer.operation_succeeded()
        return response


class TracingPolicy(Policy):
    """Wrap each attempt in a tracing span and emit per-request events.

    Re-entered once per retry attempt / redirect hop (it sits *inside* those
    wrappers), so it opens one span per attempt. Span attributes follow
    OpenTelemetry semantic conventions:

    - ``http.request.method``: HTTP method.
    - ``url.full``: Full URL (no redaction — install a separate redactor
      if you need it).
    - ``server.address`` / ``server.port``: Resolved from the URL.
    - ``http.response.status_code``: Set on success.
    - ``error.type``: Set on exception.
    - ``http.request.resend_count``: Retry attempt count from
      ``ctx.data["retry_count"]`` (when retry policy is upstream).

    While the span is open the active trace/span ids are bound into the
    correlation ``contextvars`` so downstream log records carry them, and the
    per-operation ``HttpTracer`` (from the call's
    ``instrumentation_context.http_tracer_factory``) receives the per-request
    events (``request_sent``, ``response_headers_received``,
    ``response_received``). The operation-level lifecycle
    (``operation_started`` / ``operation_succeeded`` / ``operation_failed``)
    is emitted by `OperationTracingPolicy`, which brackets the whole call from
    outside the retry / redirect wrappers.

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
        # Share one per-operation tracer with the operation / redirect / retry
        # policies via ``ctx.data`` (whichever policy runs first mints it).
        http_tracer = resolve_http_tracer(ctx)
        span = self._tracer.start_span(f"HTTP {request.method}", parent=parent)
        _set_request_attributes(span, request)
        with bind_correlation(trace_id=_trace_id(span), span_id=_span_id(span)):
            return self._dispatch(request, ctx, span, http_tracer)

    def _dispatch(
        self,
        request: Request,
        ctx: PipelineContext,
        span: Span,
        http_tracer: HttpTracer,
    ) -> Response:
        """Run the downstream chain, emitting per-attempt tracer events around it.

        The span and the ``request_sent`` / ``response_*`` events fire on every
        entry (once per attempt / hop); the operation-level outcome is reported
        separately by `OperationTracingPolicy`.
        """
        _notify_request_sent(http_tracer, request)
        try:
            with span.make_current():
                response = self.next.send(request, ctx)
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
    """Emit ``request_sent`` with the body byte count, or ``None`` if unknown.

    A bodyless request reports ``0``; a body with a known length reports that
    count; a body whose length is unknown (``content_length()`` returns ``-1``,
    e.g. a streamed upload) reports ``None`` so the event still fires and
    consumers see a symmetric request_sent stream regardless of body shape.
    """
    body = request.body
    if body is None:
        http_tracer.request_sent(0)
        return
    length = body.content_length()
    http_tracer.request_sent(length if length >= 0 else None)


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


__all__ = ["OperationTracingPolicy", "TracingPolicy"]
