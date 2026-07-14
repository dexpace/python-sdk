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


class NetworkError(SdkError, OSError):
    """A transport-level failure that never yielded a usable response.

    Roots the transport branch of the taxonomy: connection refused, DNS
    failure, TLS handshake failure, connect timeout — anything where the
    request never completed an exchange that produced a response.

    It subclasses ``OSError`` in addition to ``SdkError`` so that existing
    ``except OSError:`` sites (in user code or third-party libraries) keep
    matching transport failures after the SDK re-wraps them. The multiple
    inheritance is deliberate and safe: both bases share ``Exception``, and
    ``SdkError.__init__`` cooperates with ``OSError`` via ``super()``.

    Transport failures on this branch are *always* retryable at the
    error-class level — a request that never reached the service is safe to
    re-send on an idempotent method. This is orthogonal to the per-status
    retryability of ``HttpResponseError`` (which classifies HTTP status codes
    on a response that *was* received); here the retryability is a property of
    the error type itself. ``retryable`` is a class-level constant so the
    ``Retryable`` structural protocol reads it directly.

    Attributes:
        retryable: Always ``True`` — the transport branch never got a response,
            so a retry on an idempotent method may succeed.
    """

    retryable: bool = True


class ServiceRequestError(NetworkError):
    """The request never reached the service.

    Raised for connection failures, DNS errors, TLS handshake failures, and
    any condition that prevents the request from being sent. Safe to retry
    for idempotent methods.
    """


class ServiceRequestTimeoutError(ServiceRequestError):
    """The request timed out before any data was transmitted.

    The SDK's request-timeout type. It subclasses ``NetworkError`` (via
    ``ServiceRequestError``) explicitly, so it is an ``OSError`` and retryable
    by design — the SDK never relies on Python's builtin ``TimeoutError``
    (itself an ``OSError`` since 3.10) coincidentally lining up. Discriminate
    it before any cancellation type by subtype, never by message text.
    """


class ServiceResponseError(SdkError):
    """The request was sent but the response could not be parsed.

    Raised when the connection drops mid-response, the response framing is
    malformed, or decoding fails partway through a chunked stream. Whether
    it is safe to retry depends on the operation's idempotence.
    """


class ServiceResponseTimeoutError(ServiceResponseError):
    """The response timed out after the request was transmitted."""


class RequestCancelledError(SdkError, OSError):
    """The request was cancelled cooperatively before completion.

    The terminal type for synchronous cooperative cancellation: a caller (or a
    scheduled deadline) cancelled the in-flight request via the cooperative
    cancellation handle. Like ``NetworkError`` it subclasses ``OSError`` so
    ``except OSError:`` sites match it, but it is deliberately *not* a
    ``NetworkError`` — cancellation is terminal, never retried. Its
    ``retryable`` flag is a class-level ``False`` so the ``Retryable``
    structural protocol reads it directly and a retry loop leaves it alone.

    Discriminate it from a timeout by subtype (a ``ServiceRequestTimeoutError``
    is a retryable ``NetworkError``; this is not), never by message text — the
    two carry unrelated semantics even when their messages coincide. The
    asynchronous equivalent is ``asyncio.CancelledError``, which is already a
    ``BaseException`` and needs no SDK subtype.

    Attributes:
        retryable: Always ``False`` — cancellation is terminal.
    """

    retryable: bool = False


__all__ = [
    "NetworkError",
    "RequestCancelledError",
    "SdkError",
    "ServiceRequestError",
    "ServiceRequestTimeoutError",
    "ServiceResponseError",
    "ServiceResponseTimeoutError",
]
