"""Exceptions raised for non-2xx HTTP responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SdkError

if TYPE_CHECKING:
    from ..http.response.async_response import AsyncResponse
    from ..http.response.response import Response
    from ..http.response.status import Status

    type _AnyResponse = Response | AsyncResponse


class HttpResponseError(SdkError):
    """A 4xx or 5xx HTTP response was received intact.

    Carries the response so callers can inspect status, headers, and body.
    The body is not pre-buffered — callers should ``read()`` it before the
    response goes out of scope if they need the content.

    Attributes:
        status: HTTP status code (``Status`` enum value, ``None`` if
            constructed without a response).
        reason: HTTP reason phrase (``None`` if no response was captured).
        response: The full response object, for inspection.
        model: Optional deserialised body payload (set by consumer
            libraries when they parse the error body).
    """

    status: Status | None
    reason: str | None
    response: _AnyResponse | None
    model: Any

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
                separately for caller-supplied deserialised bodies.
        """
        self.response = response
        self.status = response.status if response is not None else None
        self.reason = response.reason if response is not None else None
        self.model = kwargs.pop("model", None)
        if message is None:
            label = self.status.name if self.status is not None else "unknown"
            message = f"Operation returned a non-success status: {label}"
        super().__init__(message, **kwargs)


class DecodeError(HttpResponseError):
    """The response body could not be decoded as the expected format."""


class ResourceExistsError(HttpResponseError):
    """The target resource already exists (typically 409 Conflict)."""


class ResourceNotFoundError(HttpResponseError):
    """The target resource does not exist (typically 404 Not Found)."""


class ResourceModifiedError(HttpResponseError):
    """The target resource was modified since a precondition was evaluated.

    Typically raised for 412 Precondition Failed on a write operation.
    """


class ResourceNotModifiedError(HttpResponseError):
    """The target resource was not modified (304 Not Modified)."""


class ClientAuthenticationError(HttpResponseError):
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
