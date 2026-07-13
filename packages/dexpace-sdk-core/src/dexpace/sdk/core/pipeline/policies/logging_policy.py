# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that emits structured request/response logs."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from ...http.request.loggable_request_body import LoggableRequestBody
from ...instrumentation import ClientLogger, UrlRedactor
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.common.media_type import MediaType
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext

#: Default byte ceiling for the request-body preview the logging policy
#: captures and emits. Deliberately small (8 KiB): a bounded diagnostic
#: preview, not a full-body mirror. The ``Loggable*`` body wrappers keep their
#: own large, independently configurable ``max_capture_bytes`` ceiling; the
#: logging policy simply requests this tighter bound so a large upload is never
#: copied whole into the log tap.
DEFAULT_PREVIEW_BYTES: Final[int] = 8 * 1024


class LoggingPolicy(Policy):
    """Emit one structured log line per request and per response.

    Logged fields include the HTTP method, redacted URL, response status,
    duration in milliseconds, and the current trace id. The URL is run
    through ``UrlRedactor`` to strip userinfo and sensitive query
    parameters. When the request carries a body, a bounded preview of it
    (capped at ``preview_bytes``, default 8 KiB) is attached to the response
    line as ``request_body_preview``; the full body still streams to the
    transport uncut — only the diagnostic tap truncates.

    Disable per-call by setting ``ctx.options["logging_enabled"] = False``.

    Attributes:
        preview_bytes: Byte ceiling on the captured request-body preview.
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
                ``dexpace.sdk.core.http``.
            redactor: URL redactor. Defaults to a stock ``UrlRedactor``.
            preview_bytes: Byte ceiling on the request-body preview.

        Raises:
            ValueError: If ``preview_bytes`` is non-positive.
        """
        if preview_bytes <= 0:
            raise ValueError(f"preview_bytes must be positive, got {preview_bytes}")
        self._logger = logger or ClientLogger("dexpace.sdk.core.http")
        self._redactor = redactor or UrlRedactor()
        self.preview_bytes = preview_bytes

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if not ctx.options.get("logging_enabled", True):
            return self.next.send(request, ctx)
        url = self._redactor.redact(str(request.url))
        trace_id = ctx.call.instrumentation_context.trace_id.value
        method = str(request.method)
        tap = self._tap_body(request)
        if tap is not None:
            request = request.with_body(tap)
        self._logger.info("http.request", method=method, url=url, trace_id=trace_id)
        started = time.monotonic()
        try:
            response = self.next.send(request, ctx)
        except BaseException as err:
            self._log_error(method, url, err, started, trace_id)
            raise
        self._log_response(method, url, response, started, trace_id, tap)
        return response

    def _tap_body(self, request: Request) -> LoggableRequestBody | None:
        """Wrap the request body so a bounded preview is captured mid-send.

        Returns ``None`` when the request has no body. An already-wrapped body
        is reused rather than nested, so a retried / redirected send does not
        stack taps.
        """
        body = request.body
        if body is None:
            return None
        if isinstance(body, LoggableRequestBody):
            return body
        return LoggableRequestBody(body, max_capture_bytes=self.preview_bytes)

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

    def _log_response(
        self,
        method: str,
        url: str,
        response: Response,
        started: float,
        trace_id: str,
        tap: LoggableRequestBody | None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        fields: dict[str, object] = {
            "method": method,
            "url": url,
            "status": int(response.status),
            "duration_ms": elapsed_ms,
            "trace_id": trace_id,
        }
        if tap is not None:
            # Bound the preview at ``preview_bytes`` even when the tap captured
            # more (a caller-supplied wrapper may carry a larger ceiling): the
            # logging preview is a small, fixed diagnostic slice regardless.
            preview = tap.snapshot(self.preview_bytes)
            fields["request_body_preview"] = _preview_text(preview, tap.media_type())
        self._logger.info("http.response", **fields)


def _preview_text(data: bytes, media_type: MediaType | None) -> str:
    """Render captured body bytes as a log-safe preview string.

    Decodes with the body's declared charset when present and recognised,
    otherwise UTF-8. Invalid or binary byte sequences are replaced with
    U+FFFD instead of raising, so the logging path never fails on a binary or
    mislabelled payload.

    Args:
        data: The captured (already length-capped) body bytes.
        media_type: The body's media type, consulted for its ``charset``.
            ``None`` (or an unrecognised charset) falls back to UTF-8.

    Returns:
        A decoded, never-raising preview string.
    """
    charset = media_type.charset if media_type is not None else None
    return data.decode(charset or "utf-8", errors="replace")


__all__ = ["DEFAULT_PREVIEW_BYTES", "LoggingPolicy"]
