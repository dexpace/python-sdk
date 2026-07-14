# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``LoggingPolicy`` that emits structured wire logs."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...http.request.loggable_request_body import LoggableRequestBody
from ...http.response.async_loggable_response_body import AsyncLoggableResponseBody
from ...instrumentation import ClientLogger, UrlRedactor
from ..async_policy import AsyncPolicy
from ..stage import Stage
from .logging_policy import DEFAULT_PREVIEW_BYTES, _preview_text

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext


class AsyncLoggingPolicy(AsyncPolicy):
    """Async twin of `LoggingPolicy`: one structured log per request/response.

    Emits the same event model as the sync policy — ``http.request`` before the
    send, ``http.response`` on success, ``http.error`` on failure — with the
    same field keys (method, redacted URL, status, duration, trace id) so log
    consumers see one naming across both pillars. The URL is redacted through
    the shared ``UrlRedactor``.

    Body previews are bounded at ``preview_bytes`` (default 8 KiB) and never
    truncate what flows onward:

    - The request body (a sync ``RequestBody`` even on the async pillar) is
      tapped with ``LoggableRequestBody`` and surfaced as
      ``request_body_preview``, exactly as the sync policy does.
    - The response body (an ``AsyncResponseBody``) is captured through the
      ``AsyncLoggableResponseBody`` two-regime drain and surfaced as
      ``response_body_preview`` — but ONLY when the response declares a content
      length. A body of unknown size is passed through untouched and left
      un-previewed, since it cannot be safely bound-previewed.

    Disable per-call by setting ``ctx.options["logging_enabled"] = False``.

    Attributes:
        preview_bytes: Byte ceiling on each captured body preview.
    """

    STAGE = Stage.LOGGING
    __slots__ = ("_logger", "_redactor", "preview_bytes")

    def __init__(
        self,
        *,
        logger: ClientLogger | None = None,
        redactor: UrlRedactor | None = None,
        preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    ) -> None:
        """Configure the policy.

        Args:
            logger: Structured logger. Defaults to one named
                ``dexpace.sdk.core.http`` (the same name the sync policy uses,
                so both pillars log under one logger).
            redactor: URL redactor. Defaults to a stock ``UrlRedactor``.
            preview_bytes: Byte ceiling on each body preview.

        Raises:
            ValueError: If ``preview_bytes`` is non-positive.
        """
        if preview_bytes <= 0:
            raise ValueError(f"preview_bytes must be positive, got {preview_bytes}")
        self._logger = logger or ClientLogger("dexpace.sdk.core.http")
        self._redactor = redactor or UrlRedactor()
        self.preview_bytes = preview_bytes

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        if not ctx.options.get("logging_enabled", True):
            return await self.next.send(request, ctx)
        url = self._redactor.redact(str(request.url))
        trace_id = ctx.call.instrumentation_context.trace_id.value
        method = str(request.method)
        tap = self._tap_request(request)
        if tap is not None:
            request = request.with_body(tap)
        self._logger.info("http.request", method=method, url=url, trace_id=trace_id)
        started = time.monotonic()
        try:
            response = await self.next.send(request, ctx)
        except BaseException as err:
            self._log_error(method, url, err, started, trace_id)
            raise
        return await self._log_response(method, url, response, started, trace_id, tap)

    def _tap_request(self, request: Request) -> LoggableRequestBody | None:
        """Wrap the request body so a bounded preview is captured mid-send.

        Returns ``None`` when the request has no body. An already-wrapped body
        is reused rather than nested, so a retried send does not stack taps.
        """
        body = request.body
        if body is None:
            return None
        if isinstance(body, LoggableRequestBody):
            return body
        return LoggableRequestBody(body, max_capture_bytes=self.preview_bytes)

    def _tap_response(self, response: AsyncResponse) -> AsyncLoggableResponseBody | None:
        """Wrap the response body for capture, skipping undeclared-length bodies.

        Returns ``None`` when there is no body or its content length is unknown
        (``-1``): a body whose size is not declared cannot be safely
        bound-previewed, so it is streamed to the caller untouched.
        """
        body = response.body
        if body is None or body.content_length() < 0:
            return None
        if isinstance(body, AsyncLoggableResponseBody):
            return body
        return AsyncLoggableResponseBody(body, max_capture_bytes=self.preview_bytes)

    def _log_error(
        self, method: str, url: str, err: BaseException, started: float, trace_id: str
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._logger.error(
            "http.error",
            method=method,
            url=url,
            error_type=type(err).__name__,
            duration_ms=elapsed_ms,
            trace_id=trace_id,
        )

    async def _log_response(
        self,
        method: str,
        url: str,
        response: AsyncResponse,
        started: float,
        trace_id: str,
        request_tap: LoggableRequestBody | None,
    ) -> AsyncResponse:
        """Emit ``http.response`` with the captured previews; return the response.

        The response body may be re-wrapped for capture, so the (possibly
        wrapped) response is returned for the caller to consume.
        """
        elapsed_ms = int((time.monotonic() - started) * 1000)
        fields: dict[str, object] = {
            "method": method,
            "url": url,
            "status": int(response.status),
            "duration_ms": elapsed_ms,
            "trace_id": trace_id,
        }
        if request_tap is not None:
            preview = request_tap.snapshot(self.preview_bytes)
            fields["request_body_preview"] = _preview_text(preview, request_tap.media_type())
        response_tap = self._tap_response(response)
        if response_tap is not None:
            response = response.with_body(response_tap)
            preview = await response_tap.snapshot(self.preview_bytes)
            fields["response_body_preview"] = _preview_text(preview, response_tap.media_type())
        self._logger.info("http.response", **fields)
        return response


__all__ = ["AsyncLoggingPolicy"]
