"""Pipeline policy that opens a span around the downstream chain."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...instrumentation import NOOP_TRACER, Tracer
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.common.url import Url
    from ...http.request.request import Request
    from ...http.response.response import Response
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
        span = self._tracer.start_span(f"HTTP {request.method}", parent=parent)
        host, port = _split_host(request.url)
        span.set_attribute("http.request.method", str(request.method))
        span.set_attribute("url.full", str(request.url))
        if host:
            span.set_attribute("server.address", host)
        if port is not None:
            span.set_attribute("server.port", port)
        try:
            with span.make_current():
                response = self.next.send(request, ctx)
        except BaseException as err:
            span.set_error(type(err).__name__)
            span.end(error=err)
            raise
        span.set_attribute("http.response.status_code", int(response.status))
        retry_count = ctx.data.get("retry_count")
        if isinstance(retry_count, int) and retry_count > 0:
            span.set_attribute("http.request.resend_count", retry_count)
        span.end()
        return response


def _split_host(url: Url) -> tuple[str | None, int | None]:
    return url.host or None, url.port


__all__ = ["TracingPolicy"]
