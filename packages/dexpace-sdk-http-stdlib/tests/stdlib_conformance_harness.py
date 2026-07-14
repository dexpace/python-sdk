# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Real-transport harness for `UrllibHttpClient` / `AsyncioHttpClient`.

Unlike `fake_transport.py` (an in-memory fake native layer), this harness
drives the REAL `dexpace-sdk-http-stdlib` adapters over a REAL local TCP
server (`_MockServer`) speaking raw HTTP/1.1 — both stdlib transports refuse
to follow redirects and force ``Connection: close``, so one accepted
connection always corresponds to exactly one programmed `Outcome`.

The seam between "what the battery programs" and "what the real adapter
does" is a thin per-transport wrapper (`_ObservableUrllibClient` /
`_ObservableAsyncioClient`) that:

- claims the next programmed `Outcome` from a shared, lock-protected queue
  (atomic, so concurrent callers never race over which outcome is theirs);
- for `CONNECT_ERROR` / `CONNECT_TIMEOUT` — facets the mock server cannot
  produce, since they must never complete a TCP handshake — retargets the
  request at a closed port / a blackhole address instead of the mock server,
  reusing the exact techniques the existing per-adapter unit tests already
  rely on;
- otherwise tags the request with a unique call-id header and retargets it
  at the mock server, which looks the claimed outcome up by that id once it
  accepts the connection;
- lets the real adapter run entirely unmodified — the wrapper never touches
  its body-draining, framing, or error-mapping logic;
- recovers `sent_requests()` from what the mock server actually received off
  the wire (the most faithful proof of "what reached the native layer"),
  pairing it with the per-call timeout the wrapper resolved itself (the one
  fact a wire capture can never observe, since a socket timeout is a local
  option, not a wire artifact).

Capability flags are set honestly for what a minimal, standard-library-only
transport can and cannot simulate — see each flag's assignment below and its
docstring on `_BaseHarness` for the verified reasoning.
"""

from __future__ import annotations

import contextlib
import dataclasses
import queue
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Self

from tests.transport_conformance.harness import (
    AsyncTransportHarness,
    ClosableNative,
    DropLog,
    Outcome,
    OutcomeKind,
    ResponseSpec,
    SentRequest,
    SyncTransportHarness,
)

from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request.request import Request
from dexpace.sdk.http.stdlib.asyncio_http_client import AsyncioHttpClient
from dexpace.sdk.http.stdlib.urllib_http_client import UrllibHttpClient

if TYPE_CHECKING:
    from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
    from dexpace.sdk.core.client.http_client import HttpClient
    from dexpace.sdk.core.http.response.async_response import AsyncResponse
    from dexpace.sdk.core.http.response.response import Response

_CALL_ID_HEADER: Final = "X-Dexpace-Harness-Call-Id"
#: Nothing listens here — a real, immediate connection refusal.
_CLOSED_HOST: Final = "127.0.0.1"
_CLOSED_PORT: Final = 1
#: Non-routable (RFC 5737-adjacent, reserved by convention in this repo's own
#: existing per-adapter tests): the SYN is silently dropped, producing a
#: genuine connect-phase timeout rather than an immediate refusal.
_BLACKHOLE_HOST: Final = "10.255.255.1"
_BLACKHOLE_PORT: Final = 81
_DEFAULT_TIMEOUT: Final = 30.0
#: Effective timeout forced onto the two outcomes whose whole purpose is to
#: make the client's *own* timeout fire (CONNECT_TIMEOUT / READ_TIMEOUT).
#: Those battery tests assert only the raised error's TYPE, never a timeout
#: value or `sent.timeout`, so capping the wait keeps the certification
#: identical while turning a ~30 s stall (the default the battery configures)
#: into a sub-second one. Large enough that a real loopback connect/read never
#: races it on a loaded machine; small enough to keep the suite quick.
_TIMEOUT_PROBE_BUDGET: Final = 0.5
#: How long a native-response tracker's `closed` property waits for the mock
#: server to observe the peer closing the connection before giving up.
_CLOSE_WAIT: Final = 2.0
#: How long a READ_TIMEOUT / stalled connection is held open server-side —
#: comfortably larger than `_TIMEOUT_PROBE_BUDGET` so the client's own read
#: deadline always fires first, never this sleep.
_LONG_STALL: Final = 10.0
_MIN_TIMEOUT_GRANULARITY: Final = 1e-6


class _ConnectionTracker:
    """Tracks whether a real accepted connection has since been closed.

    `closed` is a bounded wait rather than an instant read: closing a real
    socket is asynchronous from the observer's point of view (the mock
    server notices the peer's close via its own blocking read), so a plain
    boolean read taken immediately after `response.close()` would race the
    server's own detection.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def mark_closed(self) -> None:
        """Record that the connection has closed. Idempotent."""
        self._event.set()

    @property
    def closed(self) -> bool:
        """Whether the connection closed within the bounded wait budget."""
        return self._event.wait(timeout=_CLOSE_WAIT)


@dataclass
class _Capture:
    """What the mock server actually received for one call."""

    headers: Headers = field(default_factory=Headers)
    body: bytes = b""


class _RealCore:
    """Shared programming / introspection state for one harness instance.

    Owns the `_MockServer` and everything the battery inspects through the
    harness seam: the outcome queue, sent-request log, attempt counter, and
    native-response trackers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: queue.Queue[Outcome] = queue.Queue()
        self._assigned: dict[str, Outcome] = {}
        self.sent: list[SentRequest] = []
        self.attempts = 0
        self.drop_log = DropLog()
        self.responses: list[ClosableNative] = []
        self.server = _MockServer(self)

    def program(self, outcome: Outcome) -> None:
        """Queue one outcome for the next exchange."""
        self._pending.put(outcome)

    def claim_next(self) -> Outcome:
        """Atomically dequeue the next programmed outcome for this call."""
        try:
            return self._pending.get_nowait()
        except queue.Empty:
            raise AssertionError("no transport outcome was programmed") from None

    def assign(self, call_id: str, outcome: Outcome) -> None:
        """Register ``outcome`` for the mock server to look up by ``call_id``."""
        with self._lock:
            self._assigned[call_id] = outcome

    def take_assigned(self, call_id: str) -> Outcome | None:
        """Pop the outcome assigned to ``call_id``, if any."""
        with self._lock:
            return self._assigned.pop(call_id, None)

    def record_attempt(self) -> None:
        """Count one native exchange attempt."""
        with self._lock:
            self.attempts += 1

    def record_sent(self, sent: SentRequest) -> None:
        """Record a request the mock server actually received."""
        with self._lock:
            self.sent.append(sent)

    def track_response(self, tracker: ClosableNative) -> None:
        """Register a native-response tracker for a RESPONSE-kind outcome."""
        with self._lock:
            self.responses.append(tracker)

    def shutdown(self) -> None:
        """Best-effort server teardown; safe to call more than once."""
        self.server.shutdown()


def _retarget(url: Url, host: str, port: int) -> Url:
    """Return ``url`` pointed at a different plaintext host:port, path kept."""
    return Url(scheme="http", host=host, port=port, path=url.path, query=url.query)


def _aimed_at(request: Request, host: str, port: int, timeout: float) -> Request:
    """Retarget ``request`` at ``host:port`` and pin its per-call ``timeout``.

    Pinning ``timeout`` on the request itself is how the wrapper forces the
    real adapter (built with a different constructor default) to use the
    effective value the harness resolved — the adapter reads
    ``request.timeout`` first.
    """
    return dataclasses.replace(request, url=_retarget(request.url, host, port), timeout=timeout)


def _route(core: _RealCore, request: Request, default_timeout: float) -> tuple[Request, float]:
    """Claim the next outcome and retarget ``request`` at the right peer.

    `CONNECT_ERROR` / `CONNECT_TIMEOUT` are dispatched straight at a closed
    port / a blackhole address — the mock server must never see these, since
    accepting the connection at all would contradict the outcome. Every
    other kind is tagged with a fresh call-id and pointed at the mock server,
    which resolves the tag back to the claimed outcome once it accepts the
    connection. The two timeout-probe outcomes have their effective timeout
    capped to `_TIMEOUT_PROBE_BUDGET` so the client's own deadline fires
    quickly (see that constant's rationale).

    Returns:
        The retargeted request and the effective timeout actually applied.
    """
    timeout = request.timeout if request.timeout is not None else default_timeout
    outcome = core.claim_next()
    if outcome.kind is OutcomeKind.CONNECT_ERROR:
        core.record_attempt()
        return _aimed_at(request, _CLOSED_HOST, _CLOSED_PORT, timeout), timeout
    if outcome.kind is OutcomeKind.CONNECT_TIMEOUT:
        core.record_attempt()
        budget = min(timeout, _TIMEOUT_PROBE_BUDGET)
        return _aimed_at(request, _BLACKHOLE_HOST, _BLACKHOLE_PORT, budget), budget
    budget = (
        min(timeout, _TIMEOUT_PROBE_BUDGET) if outcome.kind is OutcomeKind.READ_TIMEOUT else timeout
    )
    call_id = uuid.uuid4().hex
    core.assign(call_id, outcome)
    retargeted = _aimed_at(request, "127.0.0.1", core.server.port, budget).with_header(
        _CALL_ID_HEADER, call_id
    )
    return retargeted, budget


def _finish(core: _RealCore, request: Request, call_id: str | None, timeout: float) -> None:
    """Record `SentRequest` from the mock server's capture, if one exists.

    No capture exists when the exchange never reached the mock server at all
    (a `CONNECT_ERROR` / `CONNECT_TIMEOUT` dispatch, or a body that failed to
    drain before any connection was attempted) — matching the neutral
    contract, which likewise never asserts on `sent_requests()` for those
    scenarios.
    """
    if call_id is None:
        return
    capture = core.server.take_capture(call_id)
    if capture is None:
        return
    core.record_sent(
        SentRequest(
            method=str(request.method),
            url=request.url.wire_form(),
            headers=capture.headers,
            body=capture.body,
            timeout=timeout,
        )
    )


class _MockServer:
    """Real TCP server driving programmed outcomes for either stdlib client.

    Speaks raw HTTP/1.1 over a socket, so either `UrllibHttpClient` or
    `AsyncioHttpClient` can connect to it — the wire protocol is identical
    regardless of which client library drives the request. Every accepted
    connection serves exactly one claimed `Outcome`, matching both
    transports' one-request-per-connection (`Connection: close`) behaviour.
    """

    def __init__(self, core: _RealCore) -> None:
        self._core = core
        self._captures: dict[str, _Capture] = {}
        self._captures_lock = threading.Lock()
        handler = _make_handler(self)
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._server.allow_reuse_address = True
        self.port: int = self._server.server_address[1]
        # A small poll interval keeps `shutdown()` snappy: `serve_forever`
        # only checks the stop flag once per interval, so the stock 0.5 s
        # default would add ~0.5 s to every harness teardown (one per test).
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def take_capture(self, call_id: str) -> _Capture | None:
        """Pop the captured request for ``call_id``, if the server saw one."""
        with self._captures_lock:
            return self._captures.pop(call_id, None)

    def shutdown(self) -> None:
        """Stop accepting new connections. Idempotent; safe under GC."""
        with contextlib.suppress(Exception):
            self._server.shutdown()
            self._server.server_close()

    def handle_connection(self, rfile: object, wfile: object, connection: socket.socket) -> None:
        """Serve one accepted connection end to end."""
        call_id, headers, body = _read_request(rfile)  # type: ignore[arg-type]
        if call_id is None:
            # No harness-issued call-id: nothing we programmed could route
            # here deliberately. Drop the connection without a response.
            return
        with self._captures_lock:
            self._captures[call_id] = _Capture(headers=headers, body=body)
        outcome = self._core.take_assigned(call_id)
        if outcome is None:
            return
        self._core.record_attempt()
        if outcome.kind is OutcomeKind.RESPONSE:
            assert outcome.response is not None
            tracker = _ConnectionTracker()
            self._core.track_response(tracker)
            _write_response(wfile, outcome.response)  # type: ignore[arg-type]
            # Half-close: signals EOF to the peer's read (so a response with
            # no Content-Length still frames correctly, purely via RFC 9112
            # §6.3's close-delimited rule) while keeping our own read side
            # open, so the loop below can still observe the peer's own close.
            # Never an HTTP-level "Connection: close" header — the neutral
            # contract asserts on the exact header set a program specifies,
            # and this framing choice must stay invisible at that layer.
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_WR)
            _watch_for_close(connection, tracker)
        elif outcome.kind is OutcomeKind.READ_TIMEOUT:
            time.sleep(_LONG_STALL)
        else:
            # PROTOCOL_ERROR and CANCELLED (the latter unreachable while
            # `supports_cancellation` is False) both model a peer that speaks
            # garbage instead of a valid response.
            with contextlib.suppress(OSError):
                wfile.write(b"NOT A VALID STATUS LINE\r\n\r\n")  # type: ignore[attr-defined]


def _make_handler(server: _MockServer) -> type[socketserver.BaseRequestHandler]:
    class _Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            server.handle_connection(self.rfile, self.wfile, self.connection)

    return _Handler


def _read_request(rfile: socket.SocketIO) -> tuple[str | None, Headers, bytes]:
    """Parse one HTTP/1.1 request off the wire; return its call-id, headers, body."""
    rfile.readline()  # request line: method/path are not needed server-side
    call_id: str | None = None
    headers = Headers()
    content_length = 0
    while True:
        line = rfile.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        text = line.decode("latin-1").rstrip("\r\n")
        name, _, value = text.partition(":")
        name, value = name.strip(), value.strip()
        if name.lower() == _CALL_ID_HEADER.lower():
            call_id = value
            continue
        headers = headers.with_added(name, value)
        if name.lower() == "content-length":
            with contextlib.suppress(ValueError):
                content_length = int(value)
    body = rfile.read(content_length) if content_length else b""
    return call_id, headers, body


def _write_response(wfile: socket.SocketIO, spec: ResponseSpec) -> None:
    """Write a programmed `ResponseSpec` as a real HTTP/1.1 response.

    Writes ``spec.headers`` verbatim — never injecting a ``Content-Length``
    or ``Connection`` header of its own — so both stdlib clients read the
    exact programmed body via the TCP half-close their caller applies right
    after this returns (RFC 9112 §6.3's close-delimited framing), regardless
    of what a test's own (possibly deliberately malformed) ``spec.headers``
    claims.
    """
    reason = spec.reason if spec.reason is not None else "programmed"
    status_token = str(spec.status)
    lines: list[bytes] = [f"HTTP/1.1 {status_token} {reason}".encode("latin-1", "replace")]
    for name, value in spec.headers:
        lines.append(f"{name}: {value}".encode("latin-1", "replace"))
    body = spec.body if not spec.stream else b"".join(spec.chunks)
    lines.extend((b"", b""))
    with contextlib.suppress(OSError):
        wfile.write(b"\r\n".join(lines) + body)
        wfile.flush()


def _watch_for_close(connection: socket.socket, tracker: _ConnectionTracker) -> None:
    """Mark the tracker closed only on an *observed* peer close (EOF).

    Crucially, the tracker is NOT marked on the `_CLOSE_WAIT` `recv` timeout:
    if the adapter regressed and never released the connection, the wait must
    expire with the tracker still open so the close-cascade battery tests can
    actually fail. Marking unconditionally would make those assertions
    non-falsifiable — they would pass even against an adapter that leaked the
    socket. ``socket.timeout`` is an ``OSError``, so the suppress swallows it
    and the function returns without marking.
    """
    with contextlib.suppress(OSError):
        connection.settimeout(_CLOSE_WAIT)
        while True:
            data = connection.recv(4096)
            if not data:
                tracker.mark_closed()
                return


# --------------------------------------------------------------------------- #
# Sync wrapper + harness.
# --------------------------------------------------------------------------- #


class _ObservableUrllibClient:
    """Wraps `UrllibHttpClient`, recording what reached the mock server."""

    def __init__(self, real: UrllibHttpClient, core: _RealCore, default_timeout: float) -> None:
        self._real = real
        self._core = core
        self._default_timeout = default_timeout
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Route, delegate to the real adapter, then record what was sent.

        Checked before claiming an outcome: a closed client must raise
        without requiring a test to have programmed anything, matching the
        neutral contract's own closed-adapter tests, which never call
        ``expect_*`` at all.
        """
        if self._closed:
            raise ServiceRequestError("client is closed")
        retargeted, timeout = _route(self._core, request, self._default_timeout)
        call_id = retargeted.headers.get(_CALL_ID_HEADER)
        try:
            return self._real.execute(retargeted)
        finally:
            _finish(self._core, request, call_id, timeout)

    def close(self) -> None:
        """Close the wrapped adapter. Idempotent."""
        self._closed = True
        self._real.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class StdlibSyncHarness(SyncTransportHarness):
    """Certifies `UrllibHttpClient` against the shared conformance battery."""

    # No cancellation hook exists anywhere in `HttpClient.execute(request)` —
    # `Request` carries no cancellation token, and cooperative cancellation is
    # a pipeline/retry-loop concept in this SDK, never a transport one.
    supports_cancellation = False
    # This transport fully buffers every request body before send (see the
    # module docstring's "no streaming uploads" limitation): the exact length
    # is always known once buffering completes, so it always frames with
    # Content-Length and never emits (or needs) chunked encoding.
    supports_unknown_length_chunked_framing = False
    # `http.client` rejects a non-numeric/out-of-range status line itself,
    # before handing back any response object — and `Status` is total over
    # integers (TRANSPORT-24), so no *valid* status can fail adaptation
    # either. There is no reachable state where a native response exists yet
    # adaptation fails.
    supports_adaptation_failure = False
    # `http.client` parses response headers via `email.message.Message`,
    # whose own parser treats an invalid header name as the end of the
    # header block, silently discarding every header line that follows —
    # upstream of anything this adapter's own (per-entry, defensive) header
    # conversion controls. Verified empirically; see the module docstring
    # limitation this task's certification added to `urllib_http_client.py`.
    supports_defensive_inbound_headers = False
    # No BYO / proxy surface exists on `UrllibHttpClient` at all.
    supports_byo = False
    # No mechanism exists to make the adapter drop a header of the harness's
    # choosing; nothing here can honestly claim to simulate this facet.
    supports_native_header_rejection = False
    supports_proxy_introspection = False
    min_timeout_granularity = _MIN_TIMEOUT_GRANULARITY

    def __init__(self) -> None:
        self._core = _RealCore()

    def expect(self, outcome: Outcome) -> None:
        self._core.program(outcome)

    def sent_requests(self) -> list[SentRequest]:
        return self._core.sent

    def attempt_count(self) -> int:
        return self._core.attempts

    def drop_log(self) -> DropLog:
        return self._core.drop_log

    def native_responses(self) -> list[ClosableNative]:
        return self._core.responses

    def mark_native_rejected(self, *names: str) -> None:
        # No native-rejection mechanism exists for this transport; recorded
        # only so the (skipped, per `supports_native_header_rejection`)
        # battery tests have somewhere inert to write.
        for name in names:
            self._core.drop_log.record(name)

    def byo_underlying(self) -> ClosableNative | None:
        return None

    def new_client(self, **options: object) -> HttpClient:
        timeout = options.get("timeout", _DEFAULT_TIMEOUT)
        assert isinstance(timeout, int | float)
        real = UrllibHttpClient(timeout=float(timeout))
        return _ObservableUrllibClient(real, self._core, float(timeout))

    def close_client(self, client: HttpClient) -> None:
        assert isinstance(client, _ObservableUrllibClient)
        client.close()

    def shutdown(self) -> None:
        self._core.shutdown()


# --------------------------------------------------------------------------- #
# Async wrapper + harness.
# --------------------------------------------------------------------------- #


class _ObservableAsyncioClient:
    """Wraps `AsyncioHttpClient`, recording what reached the mock server."""

    def __init__(self, real: AsyncioHttpClient, core: _RealCore, default_timeout: float) -> None:
        self._real = real
        self._core = core
        self._default_timeout = default_timeout
        self._closed = False

    async def execute(self, request: Request) -> AsyncResponse:
        """Route, delegate to the real adapter, then record what was sent.

        Checked before claiming an outcome — see `_ObservableUrllibClient`.
        The check runs inside the coroutine body, so a closed client still
        surfaces the failure through the returned awaitable rather than
        synchronously at call time.
        """
        if self._closed:
            raise ServiceRequestError("client is closed")
        retargeted, timeout = _route(self._core, request, self._default_timeout)
        call_id = retargeted.headers.get(_CALL_ID_HEADER)
        try:
            return await self._real.execute(retargeted)
        finally:
            _finish(self._core, request, call_id, timeout)

    async def aclose(self) -> None:
        """Close the wrapped adapter. Idempotent."""
        self._closed = True
        await self._real.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class StdlibAsyncHarness(AsyncTransportHarness):
    """Certifies `AsyncioHttpClient` against the shared conformance battery."""

    # See `StdlibSyncHarness` — no cancellation hook exists on the transport
    # seam at all; kept honestly False even though no async battery test
    # currently exercises `expect_cancellation()`.
    supports_cancellation = False
    supports_unknown_length_chunked_framing = False
    # Unlike urllib/http.client, this transport parses the status line itself
    # and rejects a non-digit token directly — a native response can
    # genuinely fail SDK-level adaptation here (verified empirically).
    supports_adaptation_failure = True
    # This transport parses headers line-by-line itself (no `email` parser
    # in the loop) and, once its own bulk-construction bug is fixed, recovers
    # a good header on either side of a malformed one (verified empirically)
    # — unlike the urllib/http.client pillar.
    supports_defensive_inbound_headers = True
    # This transport fully materialises the response body (`_read_body`
    # reads it to completion) before `execute()` ever returns — there is no
    # lazy/incremental read surface, so the streaming-dependent facets
    # (close-cascade timing, cancellation-during-body-read) cannot be
    # exercised. `asyncio.CancelledError` propagating natively through a
    # stalled `execute()` call itself IS covered — see
    # `test_cancelled_task_propagates_natively_and_closes_connection` in
    # this package's own `test_asyncio_http_client.py`.
    supports_streaming = False
    supports_byo = False
    supports_native_header_rejection = False
    supports_proxy_introspection = False
    min_timeout_granularity = _MIN_TIMEOUT_GRANULARITY

    def __init__(self) -> None:
        self._core = _RealCore()

    def expect(self, outcome: Outcome) -> None:
        self._core.program(outcome)

    def sent_requests(self) -> list[SentRequest]:
        return self._core.sent

    def attempt_count(self) -> int:
        return self._core.attempts

    def drop_log(self) -> DropLog:
        return self._core.drop_log

    def native_responses(self) -> list[ClosableNative]:
        return self._core.responses

    def mark_native_rejected(self, *names: str) -> None:
        for name in names:
            self._core.drop_log.record(name)

    def byo_underlying(self) -> ClosableNative | None:
        return None

    def new_async_client(self, **options: object) -> AsyncHttpClient:
        timeout = options.get("timeout", _DEFAULT_TIMEOUT)
        assert isinstance(timeout, int | float)
        real = AsyncioHttpClient(timeout=float(timeout))
        return _ObservableAsyncioClient(real, self._core, float(timeout))

    async def aclose_client(self, client: AsyncHttpClient) -> None:
        assert isinstance(client, _ObservableAsyncioClient)
        await client.aclose()

    def shutdown(self) -> None:
        self._core.shutdown()


__all__ = ["StdlibAsyncHarness", "StdlibSyncHarness"]
