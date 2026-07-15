# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that emits structured request/response logs."""

from __future__ import annotations

import time
from enum import Enum
from typing import TYPE_CHECKING, Final

from ...http.request.loggable_request_body import LoggableRequestBody
from ...instrumentation import ClientLogger, HeaderRedactor, UrlRedactor
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ...http.common.headers import Headers
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

#: Stable structured event names emitted by the logging policies. A failure
#: reuses ``http.response`` (carrying ``error.type``) so a log consumer sees one
#: response event whether the exchange succeeded or failed.
_REQUEST_EVENT: Final[str] = "http.request"
_RESPONSE_EVENT: Final[str] = "http.response"

#: Field-key prefixes for logged request / response headers.
_REQUEST_HEADER_PREFIX: Final[str] = "http.request.header."
_RESPONSE_HEADER_PREFIX: Final[str] = "http.response.header."


class HttpLogDetailLevel(Enum):
    """Selectable granularity for HTTP wire logging.

    ``NONE`` emits no request/response log events at all. ``HEADERS`` emits the
    events with allow-listed header fields but captures no body. ``BODY`` adds a
    bounded body preview on top of the headers. Span lifecycle and metric
    recording are owned by the tracing policy, not this one, so they run on every
    request independent of this level — ``NONE`` silences log events without
    disabling tracing or metrics.
    """

    NONE = "none"
    HEADERS = "headers"
    BODY = "body"

    @property
    def logs_events(self) -> bool:
        """True when request/response log events are emitted at this level."""
        return self is not HttpLogDetailLevel.NONE

    @property
    def logs_body(self) -> bool:
        """True only at ``BODY`` level, where a body preview is captured."""
        return self is HttpLogDetailLevel.BODY


class LoggingPolicy(Policy):
    """Emit one structured log line per request and per response.

    Emitted fields use a stable vocabulary: ``http.request.method``,
    ``url.full`` (always the redacted URL), ``http.response.status_code``,
    ``http.response.duration_ms``, allow-listed request/response header fields,
    and — at ``BODY`` level — a bounded ``request_body_preview``. A failed
    exchange emits an ``http.response`` event carrying ``error.type`` and the
    exception cause. The URL is run through ``UrlRedactor`` to strip userinfo
    and sensitive query parameters; URL-valued response headers (``Location`` /
    ``Content-Location``) are redacted through the shared ``HeaderRedactor``.

    The ``detail_level`` selects granularity: ``NONE`` emits nothing (tracing
    and metrics still run via the tracing policy), ``HEADERS`` emits events and
    header fields with no body capture, ``BODY`` adds the body preview. It
    defaults to ``NONE`` (no request/response log events until the caller opts
    into ``HEADERS`` or ``BODY``); span and metrics emission are unaffected by
    the level.

    Disable per-call by setting ``ctx.options["logging_enabled"] = False``.

    Attributes:
        detail_level: The logging granularity (``NONE`` / ``HEADERS`` / ``BODY``).
        preview_bytes: Byte ceiling on the captured request-body preview.
    """

    STAGE = Stage.LOGGING
    __slots__ = ("_header_redactor", "_logger", "_redactor", "detail_level", "preview_bytes")

    def __init__(
        self,
        *,
        logger: ClientLogger | None = None,
        redactor: UrlRedactor | None = None,
        header_redactor: HeaderRedactor | None = None,
        detail_level: HttpLogDetailLevel = HttpLogDetailLevel.NONE,
        preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    ) -> None:
        """Configure the policy.

        Args:
            logger: Structured logger. Defaults to one named
                ``dexpace.sdk.core.http``.
            redactor: URL redactor. Defaults to a stock ``UrlRedactor``.
            header_redactor: Header-name allow-list + URL-valued-header redactor,
                shared with the async policy. Defaults to a stock
                ``HeaderRedactor``.
            detail_level: Logging granularity. Defaults to ``NONE`` (no
                request/response log events until the caller opts into
                ``HEADERS`` or ``BODY``); span and metrics emission are
                unaffected by the level.
            preview_bytes: Byte ceiling on the request-body preview.

        Raises:
            ValueError: If ``preview_bytes`` is non-positive.
        """
        if preview_bytes <= 0:
            raise ValueError(f"preview_bytes must be positive, got {preview_bytes}")
        self._logger = logger or ClientLogger("dexpace.sdk.core.http")
        self._redactor = redactor or UrlRedactor()
        self._header_redactor = header_redactor or HeaderRedactor()
        self.detail_level = detail_level
        self.preview_bytes = preview_bytes

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if not self.detail_level.logs_events or not ctx.options.get("logging_enabled", True):
            return self.next.send(request, ctx)
        url = self._redactor.redact(str(request.url))
        trace_id = ctx.call.instrumentation_context.trace_id.value
        method = str(request.method)
        tap = self._tap_body(request) if self.detail_level.logs_body else None
        if tap is not None:
            request = request.with_body(tap)
        self._log_request(method, url, request.headers, trace_id)
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

    def _log_request(self, method: str, url: str, headers: Headers, trace_id: str) -> None:
        fields = _request_fields(method, url, headers, self._header_redactor)
        fields["trace_id"] = trace_id
        self._logger.info(_REQUEST_EVENT, **fields)

    def _log_error(
        self, method: str, url: str, err: BaseException, started: float, trace_id: str
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        fields = _error_fields(method, url, elapsed_ms, err)
        fields["trace_id"] = trace_id
        self._logger.error(_RESPONSE_EVENT, **fields)

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
        fields = _response_fields(
            method, url, int(response.status), elapsed_ms, response.headers, self._header_redactor
        )
        fields["trace_id"] = trace_id
        if tap is not None:
            # Bound the preview at ``preview_bytes`` even when the tap captured
            # more (a caller-supplied wrapper may carry a larger ceiling): the
            # logging preview is a small, fixed diagnostic slice regardless.
            preview = tap.snapshot(self.preview_bytes)
            fields["request_body_preview"] = _preview_text(preview, tap.media_type())
        self._logger.info(_RESPONSE_EVENT, **fields)


def _header_fields(prefix: str, redacted: Mapping[str, str]) -> dict[str, object]:
    """Prefix redacted ``name -> value`` header pairs into log field keys."""
    return {f"{prefix}{name}": value for name, value in redacted.items()}


def _request_fields(
    method: str, url: str, headers: Headers, header_redactor: HeaderRedactor
) -> dict[str, object]:
    """Build the stable ``http.request`` field set (method, url, headers)."""
    fields: dict[str, object] = {"http.request.method": method, "url.full": url}
    fields.update(_header_fields(_REQUEST_HEADER_PREFIX, header_redactor.redact(headers)))
    return fields


def _response_fields(
    method: str,
    url: str,
    status_code: int,
    duration_ms: int,
    headers: Headers,
    header_redactor: HeaderRedactor,
) -> dict[str, object]:
    """Build the stable ``http.response`` field set (excluding the body preview)."""
    fields: dict[str, object] = {
        "http.request.method": method,
        "url.full": url,
        "http.response.status_code": status_code,
        "http.response.duration_ms": duration_ms,
    }
    fields.update(_header_fields(_RESPONSE_HEADER_PREFIX, header_redactor.redact(headers)))
    return fields


def _error_fields(method: str, url: str, duration_ms: int, err: BaseException) -> dict[str, object]:
    """Build the failure ``http.response`` field set with the throwable cause."""
    return {
        "http.request.method": method,
        "url.full": url,
        "http.response.duration_ms": duration_ms,
        "error.type": type(err).__name__,
        "error.message": err,
    }


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


__all__ = ["DEFAULT_PREVIEW_BYTES", "HttpLogDetailLevel", "LoggingPolicy"]
