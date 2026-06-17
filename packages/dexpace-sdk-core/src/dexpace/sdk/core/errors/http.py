# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Exceptions raised for non-2xx HTTP responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar

from ..http.response.loggable_response_body import LoggableResponseBody
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

# Status codes for which a retry is worthwhile by default: request timeout,
# rate limiting, and the transient 5xx family. Mirrors the retry policy's
# ``_DEFAULT_STATUS_RETRIES`` so ``retryable`` and the policy agree out of
# the box; callers can override per error via the ``retryable`` kwarg.
_DEFAULT_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})


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
            the response status by default (request timeout, rate limiting,
            and transient 5xx are retryable) so the retry policy can read the
            flag directly instead of re-deriving it; callers may override it
            explicitly via the ``retryable`` constructor keyword.
    """

    status: Status | None
    reason: str | None
    response: _AnyResponse | None
    model: ModelT | None
    retryable: bool

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

        Returns:
            ``True`` when the captured status is one of the default
            retryable codes, ``False`` when no status was captured.
        """
        return self.status is not None and int(self.status) in _DEFAULT_RETRYABLE_STATUS

    def body_snapshot(self, max_bytes: int | None = None) -> bytes:
        """Preview the error response body without consuming it.

        For a LoggableResponseBody, the snapshot drains and caches the inner
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
    "ClientAuthenticationError",
    "DecodeError",
    "HttpResponseError",
    "ResourceExistsError",
    "ResourceModifiedError",
    "ResourceNotFoundError",
    "ResourceNotModifiedError",
]
