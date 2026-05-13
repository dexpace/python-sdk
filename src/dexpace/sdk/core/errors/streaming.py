"""Exceptions raised for body / stream lifecycle violations."""
from __future__ import annotations

from .base import SdkError


class StreamConsumedError(SdkError):
    """The body was already consumed."""

    def __init__(self) -> None:
        super().__init__(
            "Body stream has already been consumed. Wrap with a Loggable body "
            "decorator if you need repeatable reads."
        )


class StreamClosedError(SdkError):
    """The body was closed before any bytes were read."""

    def __init__(self) -> None:
        super().__init__(
            "Body stream is closed. The underlying transport handle has been "
            "released — call .bytes() / .iter_bytes() before closing the body."
        )


class ResponseNotReadError(SdkError):
    """The response body must be read before this attribute can be accessed."""

    def __init__(self) -> None:
        super().__init__(
            "Response body has not been read. Call .bytes(), .string(), or "
            "iterate .iter_bytes() before accessing this attribute."
        )


class StreamingError(SdkError):
    """Stream framing / decode error (e.g. SSE line too long, partial
    UTF-8 codepoint at EOF)."""


__all__ = [
    "ResponseNotReadError",
    "StreamClosedError",
    "StreamConsumedError",
    "StreamingError",
]
