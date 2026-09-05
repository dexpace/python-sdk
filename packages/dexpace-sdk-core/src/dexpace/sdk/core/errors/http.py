# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Exceptions raised for non-2xx HTTP responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar

from ..http.response.loggable_response_body import LoggableResponseBody
from ..http.response.response_body import ResponseBody
from .base import SdkError

if TYPE_CHECKING:
    from ..http.response.async_response import AsyncResponse
    from ..http.response.response import Response
    from ..http.response.status import Status

    type _AnyResponse = Response | AsyncResponse

    # PEP 696 type-parameter defaults are accepted by mypy under
    # ``python_version = "3.12"`` from the typing stub but the runtime
    # ``typing.TypeVar`` only grew the ``default`` kwarg in 3.13. Declare
    # the default only when type-checking so the source still imports on
    # 3.12 while ``HttpResponseError`` (unparametrised) keeps its historical
    # ``HttpResponseError[Any]`` meaning for downstream type-checkers.
    ModelT = TypeVar("ModelT", default=Any)
else:
    ModelT = TypeVar("ModelT")

#: Default ceiling, in bytes, on the replayable copy of an error response body
#: captured by ``HttpResponseError.read_body``. One MiB is generous for the
#: JSON / text error payloads services return while bounding the memory a single
#: failed request can pin for post-mortem inspection.
ERROR_BODY_MAX_BYTES: Final[int] = 1 << 20  # 1 MiB


# UP046 wants PEP 695 ``class Foo[T = Any](...)`` form, but that syntax
# requires Python 3.13+ at runtime; we still support 3.12.
class HttpResponseError(SdkError, Generic[ModelT]):  # noqa: UP046
    """A 4xx or 5xx HTTP response was received intact.

    Carries the response so callers can inspect status, headers, and body.
    The body is not pre-buffered — callers should ``read()`` it before the
    response goes out of scope if they need the content.

    Generic over ``ModelT`` — the deserialised body payload type. Defaults
    to ``Any`` for unparametrised use so existing callers keep their loose
    typing; consumers that always decode a known schema can write
    ``HttpResponseError[MyModel]`` to get a typed ``model`` attribute.

    Attributes:
        status: HTTP status code (``Status`` enum value, ``None`` if
            constructed without a response).
        reason: HTTP reason phrase (``None`` if no response was captured).
        response: The full response object, for inspection.
        model: Optional deserialised body payload (set by consumer
            libraries when they parse the error body). Typed as
            ``ModelT | None``.
        retryable: Whether retrying the request might succeed. Derived from
            the response status by default via the single shared classifier
            (408, 429, and every 5xx EXCEPT 501 and 505 are retryable) so the
            baked flag is computed once at construction and always agrees with
            that classifier; callers may override it explicitly via the
            ``retryable`` constructor keyword.
    """

    status: Status | None
    reason: str | None
    response: _AnyResponse | None
    model: ModelT | None
    retryable: bool
    _captured_body: bytes | None

    def __init__(
        self,
        message: object | None = None,
        response: _AnyResponse | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise from a response.

        Args:
            message: Optional human-readable description. When ``None``, a
                generic message is built from the response status.
            response: The HTTP response that triggered the error.
            **kwargs: Forwarded to ``SdkError`` (``error``,
                ``continuation_token``). The ``model`` key is consumed
                separately for caller-supplied deserialised bodies. The
                ``retryable`` key, if given, overrides the status-derived
                default (pass ``True``/``False`` to force it).
        """
        self.response = response
        self.status = response.status if response is not None else None
        self.reason = response.reason if response is not None else None
        self.model = kwargs.pop("model", None)
        self._captured_body = None
        retryable_override = kwargs.pop("retryable", None)
        self.retryable = (
            self._status_is_retryable() if retryable_override is None else bool(retryable_override)
        )
        if message is None:
            label = self.status.name if self.status is not None else "unknown"
            message = f"Operation returned a non-success status: {label}"
        super().__init__(message, **kwargs)

    def _status_is_retryable(self) -> bool:
        """Return whether this error's status is retryable by default.

        Consults the single shared canonical classifier
        (``default_status_is_retryable``: 408, 429, and every 5xx except 501
        and 505), so the flag is baked once from one definition instead of a
        hand-mirrored literal and always agrees with that classifier. The
        classifier is imported lazily inside the method because it lives under
        ``pipeline`` (whose package import pulls in policies that import this
        error module) — a module-level import would form an errors -> pipeline
        -> errors cycle.

        Returns:
            ``True`` when the captured status is 408, 429, or a 5xx other than
            501 / 505; ``False`` when no status was captured.
        """
        from ..pipeline._pure.classifier import default_status_is_retryable

        return default_status_is_retryable(self.status)

    def body_snapshot(self, max_bytes: int | None = None) -> bytes:
        """Preview the error response body without consuming it.

        For a ``LoggableResponseBody``, the snapshot drains and caches the inner
        body on the first access (this initial read is synchronous and
        may incur I/O, but subsequent reads are repeatable and fast); for
        any other body — or when no response/body is present — an empty
        ``bytes`` is returned rather than destroying the payload.

        Args:
            max_bytes: If given, return at most this many bytes from the
                front of the captured body. ``None`` returns the full
                capture.

        Returns:
            The captured body bytes, optionally truncated to ``max_bytes``;
            empty when no non-consuming preview is available.

        Raises:
            ValueError: If ``max_bytes`` is negative.
        """
        if max_bytes is not None and max_bytes < 0:
            raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
        body = self.response.body if self.response is not None else None
        if isinstance(body, LoggableResponseBody):
            return body.snapshot(max_bytes)
        return b""

    def read_body(self, max_bytes: int = ERROR_BODY_MAX_BYTES) -> bytes:
        """Read a bounded, replayable copy of the error response body.

        Unlike ``body_snapshot`` — a non-consuming preview limited to bodies
        already wrapped for repeatable reads — this drains the response body
        once, up to ``max_bytes``, and caches the result on the error. Every
        later call replays the cached copy without re-draining, so the bounded
        body is readable repeatedly for logging and post-mortem inspection.

        The first read fixes the captured bytes; a single-use response body is
        consumed by that read (call ``body_snapshot`` first if a non-consuming
        preview is needed). An already-repeatable ``LoggableResponseBody`` is
        left readable. Async response bodies and the no-response case return
        empty bytes rather than draining anything.

        Args:
            max_bytes: Ceiling on the captured copy. Defaults to the documented
                ``ERROR_BODY_MAX_BYTES`` (1 MiB); larger bodies are truncated
                to this prefix.

        Returns:
            The captured body bytes, truncated to ``max_bytes``; empty when no
            synchronously-readable body is present.

        Raises:
            ValueError: If ``max_bytes`` is negative.
        """
        if max_bytes < 0:
            raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
        if self._captured_body is None:
            self._captured_body = self._capture_body(max_bytes)
        return bytes(memoryview(self._captured_body)[:max_bytes])

    def _capture_body(self, max_bytes: int) -> bytes:
        """Drain a bounded prefix of the response body for one-time capture.

        Args:
            max_bytes: Ceiling on the bytes to capture.

        Returns:
            The captured prefix, or empty bytes when there is no
            synchronously-readable body.
        """
        body = self.response.body if self.response is not None else None
        if max_bytes == 0 or not isinstance(body, ResponseBody):
            return b""
        if isinstance(body, LoggableResponseBody):
            return body.snapshot(max_bytes)
        capture = LoggableResponseBody(body, max_capture_bytes=max_bytes)
        try:
            return capture.snapshot(max_bytes)
        finally:
            capture.close()


class DecodeError(HttpResponseError[ModelT]):
    """The response body could not be decoded as the expected format."""


class ResourceExistsError(HttpResponseError[ModelT]):
    """The target resource already exists (typically 409 Conflict)."""


class ResourceNotFoundError(HttpResponseError[ModelT]):
    """The target resource does not exist (typically 404 Not Found)."""


class ResourceModifiedError(HttpResponseError[ModelT]):
    """The target resource was modified since a precondition was evaluated.

    Typically raised for 412 Precondition Failed on a write operation.
    """


class ResourceNotModifiedError(HttpResponseError[ModelT]):
    """The target resource was not modified (304 Not Modified)."""


class ClientAuthenticationError(HttpResponseError[ModelT]):
    """Authentication failed (401) or was refused (403).

    Bearer-token policies short-circuit retry on this error — the request
    cannot succeed without new credentials.
    """


__all__ = [
    "ERROR_BODY_MAX_BYTES",
    "ClientAuthenticationError",
    "DecodeError",
    "HttpResponseError",
    "ResourceExistsError",
    "ResourceModifiedError",
    "ResourceNotFoundError",
    "ResourceNotModifiedError",
]
