""":class:`HttpClient` Protocol."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..http.request import Request
    from ..http.response import Response


@runtime_checkable
class HttpClient(Protocol):
    """Transport seam: send one :class:`Request`, get one :class:`Response`.

    The SDK is an HTTP-client *toolkit*, not an HTTP client — consuming
    libraries plug in a transport by implementing this single-method Protocol.
    Pipelines, retry, auth, and logging are all built on top.

    Implementations are expected to be safe for concurrent calls from multiple
    threads. Per-request state must be confined to local variables or to the
    returned :class:`Response` graph. The response body is not pre-buffered —
    callers are responsible for closing it.
    """

    def execute(self, request: Request) -> Response: ...


__all__ = ["HttpClient"]
