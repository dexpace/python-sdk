"""Synchronous reference ``HttpClient`` implementation built on ``urllib.request``.

Not for production traffic — it is the example/test transport that ships
with ``core``. Production deployments should plug in an adapter built on a
real HTTP library (httpx, requests, aiohttp) instead.

Honours :class:`RequestBody`'s ``iter_bytes`` for outbound payloads and
exposes the response body as a :class:`ResponseBody.from_stream` wrapper so
streaming reads are possible. Maps urllib's exception types into the SDK
error hierarchy.
"""

from __future__ import annotations

from socket import timeout as _SocketTimeout  # noqa: N812 — re-exporting the stdlib lowercase name
from types import TracebackType
from typing import Final, Self
from urllib.error import HTTPError, URLError
from urllib.request import Request as _UrllibRequest
from urllib.request import urlopen

from ..errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from ..http.common.headers import Headers
from ..http.common.protocol import Protocol
from ..http.request.request import Request
from ..http.response.response import Response
from ..http.response.response_body import ResponseBody
from ..http.response.status import Status

_DEFAULT_TIMEOUT: Final[float] = 30.0


class UrllibHttpClient:
    """Reference synchronous transport over ``urllib.request``.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit. The implementation prepares an ``urllib.Request`` per
    call, streams the response into a buffered ``ResponseBody``, and maps
    urllib failure modes into the SDK error hierarchy.

    Attributes:
        timeout: Connect/read timeout in seconds applied to ``urlopen``.
    """

    __slots__ = ("_closed", "timeout")

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        Raises:
            ServiceRequestError: When the connection cannot be established.
            ServiceResponseError: When reading the response fails.
        """
        if self._closed:
            raise ServiceRequestError("UrllibHttpClient is closed")
        raw = _build_urllib_request(request)
        try:
            opened = urlopen(raw, timeout=self.timeout)
        except TimeoutError as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except HTTPError as err:
            # urllib raises HTTPError for 4xx/5xx; surface them via the
            # normal Response path so policies (retry, error_map) can react.
            return _build_response(request, err)
        except URLError as err:
            if isinstance(err.reason, _SocketTimeout):
                raise ServiceResponseTimeoutError(str(err), error=err) from err
            raise ServiceRequestError(str(err), error=err) from err
        return _build_response(request, opened)

    def close(self) -> None:
        """Mark the client as closed. Subsequent calls raise."""
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _build_urllib_request(request: Request) -> _UrllibRequest:
    body_bytes: bytes | None = None
    if request.body is not None:
        body_bytes = b"".join(request.body.iter_bytes())
    # ``urllib.request.Request`` accepts a ``Mapping[str, str]`` only, so
    # multi-value headers are joined into a single comma-separated string
    # rather than dropped. ``Set-Cookie`` is the one header where comma
    # joining is wire-incorrect; outbound requests don't carry Set-Cookie
    # so the simple join is safe here.
    headers = {name: ", ".join(values) for name, values in request.headers.items()}
    return _UrllibRequest(
        url=request.url.wire_form(),
        data=body_bytes,
        headers=headers,
        method=str(request.method),
    )


def _build_response(request: Request, opened: object) -> Response:
    status_code: int = getattr(opened, "status", 200)
    try:
        status = Status(status_code)
    except ValueError as err:
        raise ServiceResponseError(f"Unknown status code: {status_code}", error=err) from err
    raw_headers = getattr(opened, "headers", None)
    headers = _convert_headers(raw_headers)
    body = ResponseBody.from_stream(opened)  # type: ignore[arg-type]  # urllib's HTTPResponse satisfies BinaryIO
    reason = getattr(opened, "reason", None)
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        headers=headers,
        message=reason,
        body=body,
    )


def _convert_headers(raw: object) -> Headers:
    if raw is None:
        return Headers()
    items_method = getattr(raw, "items", None)
    if items_method is None:
        return Headers()
    return Headers(list(items_method()))


__all__ = ["UrllibHttpClient"]
