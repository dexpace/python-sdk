# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Top-of-hierarchy exception types."""

from __future__ import annotations

import sys
from types import TracebackType


class SdkError(Exception):
    """Root of the SDK exception hierarchy.

    Captures the active ``sys.exc_info()`` tuple at construction time so the
    original cause is preserved even when the SDK re-wraps a stdlib
    exception. Carries an optional ``inner_exception`` (the underlying cause)
    and ``continuation_token`` (used by pagers to resume from the failed
    page).

    Attributes:
        message: Human-readable description.
        inner_exception: Underlying cause, if any.
        exc_type: ``sys.exc_info()[0]`` at construction time.
        exc_value: ``sys.exc_info()[1]`` at construction time.
        exc_traceback: ``sys.exc_info()[2]`` at construction time.
        continuation_token: Pager continuation token, if applicable.
    """

    message: str
    inner_exception: BaseException | None
    exc_type: type[BaseException] | None
    exc_value: BaseException | None
    exc_traceback: TracebackType | None
    continuation_token: str | None

    def __init__(
        self,
        message: object = "",
        *,
        error: BaseException | None = None,
        continuation_token: str | None = None,
    ) -> None:
        """Initialise the exception.

        Args:
            message: Human-readable description (stringified by ``__init__``).
            error: Optional underlying cause.
            continuation_token: Optional pager continuation token.
        """
        self.inner_exception = error
        exc_info = sys.exc_info()
        self.exc_type = exc_info[0] or (type(error) if error is not None else None)
        self.exc_value = exc_info[1] or error
        self.exc_traceback = exc_info[2] or (error.__traceback__ if error is not None else None)
        self.message = str(message)
        self.continuation_token = continuation_token
        super().__init__(self.message)


class ServiceRequestError(SdkError):
    """The request never reached the service.

    Raised for connection failures, DNS errors, TLS handshake failures, and
    any condition that prevents the request from being sent. Safe to retry
    for idempotent methods.
    """


class ServiceRequestTimeoutError(ServiceRequestError):
    """The request timed out before any data was transmitted."""


class ServiceResponseError(SdkError):
    """The request was sent but the response could not be parsed.

    Raised when the connection drops mid-response, the response framing is
    malformed, or decoding fails partway through a chunked stream. Whether
    it is safe to retry depends on the operation's idempotence.
    """


class ServiceResponseTimeoutError(ServiceResponseError):
    """The response timed out after the request was transmitted."""


__all__ = [
    "SdkError",
    "ServiceRequestError",
    "ServiceRequestTimeoutError",
    "ServiceResponseError",
    "ServiceResponseTimeoutError",
]
