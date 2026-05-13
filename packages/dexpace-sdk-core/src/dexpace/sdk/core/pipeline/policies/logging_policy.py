"""Pipeline policy that emits structured request/response logs."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...instrumentation import ClientLogger, UrlRedactor
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext


class LoggingPolicy(Policy):
    """Emit one structured log line per request and per response.

    Logged fields include the HTTP method, redacted URL, response status,
    duration in milliseconds, and the current trace id. The URL is run
    through ``UrlRedactor`` to strip userinfo and sensitive query
    parameters.

    Disable per-call by setting ``ctx.options["logging_enabled"] = False``.
    """

    STAGE = Stage.LOGGING
    __slots__ = ("_logger", "_redactor")

    def __init__(
        self,
        *,
        logger: ClientLogger | None = None,
        redactor: UrlRedactor | None = None,
    ) -> None:
        self._logger = logger or ClientLogger("dexpace.sdk.core.http")
        self._redactor = redactor or UrlRedactor()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if not ctx.options.get("logging_enabled", True):
            return self.next.send(request, ctx)
        url = self._redactor.redact(str(request.url))
        trace_id = ctx.call.instrumentation_context.trace_id.value
        self._logger.info(
            "http.request",
            method=str(request.method),
            url=url,
            trace_id=trace_id,
        )
        started = time.monotonic()
        try:
            response = self.next.send(request, ctx)
        except BaseException as err:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._logger.error(
                "http.error",
                method=str(request.method),
                url=url,
                error_type=type(err).__name__,
                duration_ms=elapsed_ms,
                trace_id=trace_id,
            )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._logger.info(
            "http.response",
            method=str(request.method),
            url=url,
            status=int(response.status),
            duration_ms=elapsed_ms,
            trace_id=trace_id,
        )
        return response


__all__ = ["LoggingPolicy"]
