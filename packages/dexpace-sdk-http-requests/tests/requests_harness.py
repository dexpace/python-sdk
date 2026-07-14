# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Real-transport harness certifying ``RequestsHttpClient`` against the shared
transport-adapter conformance battery (see
``dexpace-sdk-core/tests/transport_conformance``).

Unlike ``fake_transport.py`` (an in-memory reference adapter over a fake
native layer), this harness drives the REAL ``RequestsHttpClient`` over a REAL
``requests.Session``. The "native layer" the battery programs is realised by
mounting a custom ``requests.adapters.BaseAdapter`` on the session — the same,
already-established technique this package's own unit tests use
(``test_requests_client.py``'s ``_FixedStatusAdapter`` / ``_CapturingAdapter``
/ ``_BodyFailureAdapter``) to control exactly what a ``requests.Session``
hands back without touching a real socket. Everything ABOVE that seam —
``PreparedRequest`` construction, header merging, content-type/framing
resolution, redirect suppression, timeout plumbing, exception mapping, and
body/stream adaptation — is the real, production `RequestsHttpClient` code
path.

Two facets genuinely have no analogue for this transport, and the harness
opts out of them honestly via capability flags rather than faking support:

- ``supports_native_header_rejection``: ``requests`` does not reject any
  well-formed outbound header at the transport layer, so there is nothing for
  the adapter to defensively drop.
- ``supports_proxy_introspection``: this adapter does not (yet) expose a
  ``proxy_url`` / proxy-capability surface.

Cooperative cancellation (``OutcomeKind.CANCELLED``) has no native ``requests``
exception type — a purely synchronous, blocking call has no in-band way to
signal "an external party aborted this." The harness realises it via a real,
importable stdlib signal for exactly that situation, ``InterruptedError`` (a
blocking syscall aborted by an external signal), which `RequestsHttpClient`
now maps to ``RequestCancelledError``.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Final, cast

import requests
import requests.adapters
from tests.transport_conformance.harness import (
    ClosableNative,
    DropLog,
    Outcome,
    OutcomeKind,
    ResponseSpec,
    SentRequest,
    SyncTransportHarness,
)
from urllib3 import HTTPHeaderDict

from dexpace.sdk.core.http.common.headers import Headers

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from dexpace.sdk.core.client.http_client import HttpClient

from dexpace.sdk.http.requests import RequestsHttpClient

_DEFAULT_TIMEOUT: Final[float] = 30.0

#: Maps a ``ResponseSpec.protocol`` token onto urllib3's integer convention
#: (``raw.raw.version``), mirroring what a real urllib3 connection reports.
_PROTOCOL_VERSION: Final[dict[str, int]] = {"http/1.0": 10, "http/1.1": 11, "http/2": 20}


class _ClosedTracker:
    """A tiny ``ClosableNative`` the harness flips on ``close()``."""

    __slots__ = ("_closed",)

    def __init__(self) -> None:
        self._closed = False

    def mark_closed(self) -> None:
        """Record that the tracked native resource was closed."""
        self._closed = True

    @property
    def closed(self) -> bool:
        """Whether the tracked native resource has been closed."""
        return self._closed


class _RealNativeCore:
    """Thread-safe programmable state shared by the harness and its adapter."""

    def __init__(self) -> None:
        self._queue: deque[Outcome] = deque()
        self.sent: list[SentRequest] = []
        self.attempts = 0
        self.drop_log = DropLog()
        self.responses: list[ClosableNative] = []
        self._lock = threading.Lock()

    def program(self, outcome: Outcome) -> None:
        """Queue one outcome for the next exchange."""
        with self._lock:
            self._queue.append(outcome)

    def take(self) -> Outcome:
        """Consume the next programmed outcome, counting the attempt."""
        with self._lock:
            self.attempts += 1
            if not self._queue:
                raise AssertionError("no transport outcome was programmed")
            return self._queue.popleft()

    def record_sent(self, sent: SentRequest) -> None:
        """Record a request that reached the native layer."""
        with self._lock:
            self.sent.append(sent)


def _header_value_str(value: object) -> str:
    """Coerce a ``PreparedRequest.headers`` value to ``str``.

    ``requests`` occasionally stores a header value as ``bytes``; the SDK's
    `Headers` model is string-only, so decode defensively.

    Args:
        value: The raw header value.

    Returns:
        The value as ``str``.
    """
    return value.decode("latin-1") if isinstance(value, bytes) else str(value)


def _drain_prepared_body(body: object) -> bytes:
    """Read a ``requests.PreparedRequest.body`` into bytes.

    Mirrors the pattern already proven in this package's own unit tests
    (``_CapturingAdapter``): a sized body arrives as the caller's original
    file-like / iterable object, not yet materialised, so the adapter reads it
    itself. A failure here (a body whose production raises) propagates before
    any outcome is taken or recorded, matching the production adapter's own
    read-before-dispatch ordering.

    Args:
        body: The prepared request body — ``None``, ``bytes``, ``str``, a
            file-like object, or an iterable of ``bytes`` chunks.

    Returns:
        The fully-drained payload as ``bytes``.
    """
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    read = getattr(body, "read", None)
    if callable(read):
        data = read()
        return data if isinstance(data, bytes) else str(data).encode()
    return b"".join(cast("Iterable[bytes]", body))


class _SyntheticRaw:
    """A synthetic urllib3-shaped ``.raw`` body for a programmed ``requests.Response``.

    Exposes ``.stream()`` (preferred by ``Response.iter_content``), so a
    buffered body is a single chunk and a streamed one is produced lazily,
    optionally blocking after its programmed chunks until ``close()`` releases
    it — proving the same close-cascade / abandoned-stream behaviour the
    in-memory fake proves, but through a real ``requests.Response``.
    """

    def __init__(self, spec: ResponseSpec, on_close: Callable[[], None]) -> None:
        self.headers = HTTPHeaderDict(spec.headers)
        self.version = _PROTOCOL_VERSION.get(spec.protocol, 11)
        source = spec.chunks if spec.stream else ((spec.body,) if spec.body else ())
        self._chunks: deque[bytes] = deque(source)
        self._block_after_chunks = spec.block_after_chunks
        self._released = threading.Event()
        self._closed = False
        self._on_close = on_close

    def stream(self, _amt: object = None, decode_content: object = None) -> Iterator[bytes]:
        """Yield the programmed chunks, then optionally block until closed."""
        while self._chunks and not self._closed:
            yield self._chunks.popleft()
        if self._block_after_chunks and not self._closed:
            self._released.wait()

    def release_conn(self) -> None:
        """No-op: there is no real connection pool entry to release."""

    def close(self) -> None:
        """Release the synthetic raw body and unblock any waiting producer."""
        if self._closed:
            return
        self._closed = True
        self._released.set()
        self._on_close()


def _dispatch(outcome: Outcome, core: _RealNativeCore) -> requests.Response:
    """Realise a programmed `Outcome` as a native ``requests`` exchange result.

    Args:
        outcome: The programmed outcome for this exchange.
        core: The harness's native core (native responses are tracked here).

    Returns:
        A synthesized ``requests.Response`` for a ``RESPONSE`` outcome.

    Raises:
        requests.exceptions.ConnectionError: For ``CONNECT_ERROR``.
        requests.exceptions.ConnectTimeout: For ``CONNECT_TIMEOUT``.
        requests.exceptions.ReadTimeout: For ``READ_TIMEOUT``.
        InterruptedError: For ``CANCELLED`` (see module docstring).
        requests.exceptions.RequestException: For ``PROTOCOL_ERROR``.
    """
    if outcome.kind is OutcomeKind.CONNECT_ERROR:
        raise requests.exceptions.ConnectionError("simulated connect error")
    if outcome.kind is OutcomeKind.CONNECT_TIMEOUT:
        raise requests.exceptions.ConnectTimeout("simulated connect timeout")
    if outcome.kind is OutcomeKind.READ_TIMEOUT:
        raise requests.exceptions.ReadTimeout("simulated read timeout")
    if outcome.kind is OutcomeKind.CANCELLED:
        raise InterruptedError("simulated cooperative cancellation")
    if outcome.kind is OutcomeKind.PROTOCOL_ERROR:
        raise requests.exceptions.RequestException("simulated protocol error")
    assert outcome.response is not None
    return _build_native_response(outcome.response, core)


def _build_native_response(spec: ResponseSpec, core: _RealNativeCore) -> requests.Response:
    """Construct a real ``requests.Response`` carrying the programmed spec."""
    response = requests.Response()
    response.status_code = spec.status  # type: ignore[assignment]
    response.reason = spec.reason if spec.reason is not None else ""
    tracker = _ClosedTracker()
    core.responses.append(tracker)
    response.raw = _SyntheticRaw(spec, tracker.mark_closed)
    return response


class _ProgrammableAdapter(requests.adapters.BaseAdapter):
    """Mounted transport adapter realising the harness's programmed outcomes."""

    def __init__(self, core: _RealNativeCore) -> None:
        super().__init__()
        self._core = core

    def send(  # signature mirrors BaseAdapter.send
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        body_bytes = _drain_prepared_body(request.body)
        effective_timeout = timeout if isinstance(timeout, int | float) else None
        header_pairs = [(name, _header_value_str(value)) for name, value in request.headers.items()]
        sent = SentRequest(
            method=request.method or "",
            url=request.url or "",
            headers=Headers(header_pairs),
            body=body_bytes,
            timeout=effective_timeout,
        )
        self._core.record_sent(sent)
        outcome = self._core.take()
        return _dispatch(outcome, self._core)

    def close(self) -> None:
        """No-op: nothing real to release."""


class RequestsSyncHarness(SyncTransportHarness):
    """Certifies the real ``requests``-backed adapter against the sync battery."""

    supports_byo = True
    # requests does not reject any well-formed outbound header at the
    # transport layer, so there is nothing for the adapter to defensively drop.
    supports_native_header_rejection = False
    supports_streaming = True
    # This adapter does not (yet) expose a proxy_url / capability surface.
    supports_proxy_introspection = False
    min_timeout_granularity = 1e-6

    def __init__(self) -> None:
        self._core = _RealNativeCore()
        self._byo: _ClosedTracker | None = None

    def expect(self, outcome: Outcome) -> None:
        """Program the next native exchange to resolve as ``outcome``."""
        self._core.program(outcome)

    def sent_requests(self) -> list[SentRequest]:
        """Return, in order, every request the adapter handed to ``requests``."""
        return self._core.sent

    def attempt_count(self) -> int:
        """Return how many native exchanges the adapter initiated."""
        return self._core.attempts

    def drop_log(self) -> DropLog:
        """Return the (always-empty) drop log; rejection is not simulated."""
        return self._core.drop_log

    def native_responses(self) -> list[ClosableNative]:
        """Return the native response handles produced so far."""
        return self._core.responses

    def mark_native_rejected(self, *names: str) -> None:
        """No-op: ``supports_native_header_rejection`` is ``False``."""

    def byo_underlying(self) -> ClosableNative | None:
        """Return the BYO session's close-tracker, if one was built."""
        return self._byo

    def new_client(self, **options: object) -> HttpClient:
        """Build a real `RequestsHttpClient` over a session mounted to this harness."""
        timeout = options.get("timeout", _DEFAULT_TIMEOUT)
        assert isinstance(timeout, int | float)
        session = requests.Session()
        adapter = _ProgrammableAdapter(self._core)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        if bool(options.get("byo", False)):
            self._byo = _wrap_close_tracking(session)
        return RequestsHttpClient(timeout=float(timeout), session=session)

    def close_client(self, client: HttpClient) -> None:
        """Close a sync adapter via its ``close`` surface."""
        assert isinstance(client, RequestsHttpClient)
        client.close()


def _wrap_close_tracking(session: requests.Session) -> _ClosedTracker:
    """Wrap ``session.close`` so the harness can observe whether it was closed.

    Args:
        session: The BYO session to instrument.

    Returns:
        A `_ClosedTracker` reflecting whether ``session.close()`` was called.
    """
    tracker = _ClosedTracker()
    original_close = session.close

    def _close() -> None:
        tracker.mark_closed()
        original_close()

    session.close = _close  # type: ignore[method-assign]
    return tracker


__all__ = ["RequestsSyncHarness"]
