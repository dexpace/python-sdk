# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Real-``aiohttp`` harness for the shared transport-adapter conformance battery.

``AiohttpAsyncHarness`` implements `AsyncTransportHarness` (from the shared
``tests.transport_conformance`` battery built in PY-M8-01) against the REAL
`AiohttpHttpClient` adapter — not a fake. Every native exchange the battery
programs is realised as literally as aiohttp allows:

- A ``RESPONSE`` outcome with a numeric status and well-formed headers is
  served by a genuine ``aiohttp.web`` server bound to loopback; the request
  travels over a real socket and the reply is parsed by aiohttp's own wire
  parser. This is what makes ``test_adapter_does_not_follow_redirects``
  meaningful for this transport: a 302 only proves non-following if aiohttp's
  own redirect machinery was actually in the loop and chose not to act.
  ``READ_TIMEOUT`` is served by the same real server, simply never replying.
- ``CONNECT_ERROR`` and ``CONNECT_TIMEOUT``, a non-numeric status, and a
  malformed inbound header name are all synthesised at the point the adapter
  hands its request to ``aiohttp.ClientSession`` — the first two because a
  genuinely cancelled or refused real socket connect leaves a raw socket for
  ``aiohappyeyeballs`` to reclaim on its own schedule (a real, but
  independently-owned, resource-warning race this harness should not paper
  over by suppressing warnings), the latter two because no real HTTP/1.1
  exchange can carry either at all — aiohttp's own client parser rejects both
  before a live response ever reaches adapter code. All four exercise the
  adapter's real exception-mapping / adaptation code (`execute`,
  `_wrap_response`) against a duck-typed stand-in or synthetic native
  exception, without inventing a fake transport underneath it — this mirrors
  the connect-timeout technique the package's own ``test_aiohttp_client.py``
  already uses (a stub session raising ``aiohttp.ConnectionTimeoutError``
  directly) rather than something novel.

The seam is a custom `aiohttp.abc.AbstractResolver` (routes real exchanges to
the loopback server) plus an instance-level override of ``session.request``
(captures what the adapter handed to aiohttp and raises/returns the four
synthetic facets above). ``ClientSession`` explicitly warns against being
subclassed, so both seams are wired via instance-level monkeypatching rather
than a subclass.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Final

import aiohttp
from aiohttp import web
from tests.transport_conformance.harness import (
    AsyncTransportHarness,
    ClosableNative,
    DropLog,
    Outcome,
    OutcomeKind,
    ResponseSpec,
    SentRequest,
)

from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.http.aiohttp import AiohttpHttpClient

if TYPE_CHECKING:
    from dexpace.sdk.core.client.async_http_client import AsyncHttpClient

#: Default per-call timeout for harness-built clients. Short enough that the
#: connect-timeout and read-timeout outcomes (below) resolve quickly, long
#: enough that a genuine loopback round trip never spuriously trips it.
_DEFAULT_CLIENT_TIMEOUT: Final[float] = 1.0
#: How long the real server holds a READ_TIMEOUT / PROTOCOL_ERROR / CANCELLED
#: exchange open before giving up on its own. Purely a safety net: the
#: client's own (much shorter) timeout is expected to fire first every time.
_SERVER_HOLD_SECONDS: Final[float] = 5.0
#: RFC 7230 `token` charset, matching `dexpace.sdk.core.http.common.headers`.
#: A header name outside this set cannot survive a real HTTP/1.1 wire
#: round trip, so responses carrying one are served via the stub path.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class _HarnessCore:
    """Programmable queue and observation log for one harness instance.

    Shared by the resolver, the session-level request override, and the real
    server handler — whichever of the three ends up consuming a given
    exchange calls `take`.
    """

    def __init__(self) -> None:
        self._queue: deque[Outcome] = deque()
        self.sent: list[SentRequest] = []
        self.attempts = 0
        self.drop_log = DropLog()
        self.responses: list[ClosableNative] = []

    def program(self, outcome: Outcome) -> None:
        """Queue one outcome for the next exchange."""
        self._queue.append(outcome)

    def peek_kind(self) -> OutcomeKind:
        """Return the next queued outcome's kind without consuming it."""
        if not self._queue:
            raise AssertionError("no transport outcome was programmed")
        return self._queue[0].kind

    def peek_response(self) -> ResponseSpec:
        """Return the next queued RESPONSE outcome's spec without consuming it."""
        spec = self._queue[0].response
        if spec is None:
            raise AssertionError("next outcome is not a RESPONSE outcome")
        return spec

    def take(self) -> Outcome:
        """Consume the next programmed outcome, counting the attempt."""
        self.attempts += 1
        if not self._queue:
            raise AssertionError("no transport outcome was programmed")
        return self._queue.popleft()

    def record_sent(self, sent: SentRequest) -> None:
        """Record a request that reached the aiohttp session layer."""
        self.sent.append(sent)


def _needs_stub(spec: ResponseSpec) -> bool:
    """Whether ``spec`` can only be realised via the synthetic stub path.

    A non-numeric status or a header name outside the HTTP token charset
    cannot travel over a genuine HTTP/1.1 wire exchange — aiohttp's own
    parser rejects both before a live response ever reaches adapter code.
    """
    if not isinstance(spec.status, int):
        return True
    return any(not _TOKEN_RE.match(name) for name, _ in spec.headers)


async def _drain(data: object) -> bytes:
    """Fully drain a request payload (``None`` or an async byte iterator).

    Args:
        data: The ``data=`` argument the adapter handed to ``session.request``.

    Returns:
        The concatenated payload bytes (empty when ``data`` is ``None``).
    """
    if data is None:
        return b""
    if isinstance(data, AsyncIterator):
        chunks = [chunk async for chunk in data]
        return b"".join(chunks)
    raise TypeError(f"unexpected request payload type: {type(data)!r}")


def _resolve_result(hostname: str, host: str, port: int) -> aiohttp.abc.ResolveResult:
    """Build an `aiohttp.abc.ResolveResult` pinning ``hostname`` to ``host``:``port``."""
    return {
        "hostname": hostname,
        "host": host,
        "port": port,
        "family": socket.AF_INET,
        "proto": 0,
        "flags": 0,
    }


def _synthetic_connect_error() -> aiohttp.ClientConnectorError:
    """Build a `ClientConnectorError` shaped like a genuine connection refusal.

    Not routed through a real socket: a real refused-port or cancelled-connect
    attempt leaves ``aiohappyeyeballs`` to reclaim a raw socket on its own
    schedule, independent of anything this harness or the adapter controls,
    which is a distinct concern from what this facet is meant to certify
    (that the adapter maps this exception type to `ServiceRequestError`).
    """
    key = aiohttp.client_reqrep.ConnectionKey(
        host="service.test",
        port=80,
        is_ssl=False,
        ssl=False,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )
    return aiohttp.ClientConnectorError(key, OSError("connection refused (harness)"))


class _StubContent:
    """Minimal ``aiohttp.StreamReader``-shaped stand-in returning a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._read = False

    async def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


class _StubHeaders:
    """Minimal ``CIMultiDict``-shaped stand-in.

    Exposes only the two members the adapter's adaptation path
    (`_wrap_response` and its helpers) reads: ``.items()`` and ``.get()``.
    """

    def __init__(self, pairs: Sequence[tuple[str, str]]) -> None:
        self._pairs = tuple(pairs)

    def items(self) -> tuple[tuple[str, str], ...]:
        return self._pairs

    def get(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self._pairs:
            if key.lower() == lowered:
                return value
        return None


class _StubResponse:
    """Duck-typed stand-in for an `aiohttp.ClientResponse`.

    Used only for the two facets a real HTTP/1.1 wire exchange cannot
    reproduce (see module docstring). Everything the adapter's
    ``_wrap_response`` / `_StreamReaderAdapter` touches is present:
    ``status``, ``headers``, ``reason``, ``version``, ``content.read``, and a
    synchronous ``release`` whose effect ``native_responses()`` can observe.
    """

    def __init__(self, spec: ResponseSpec) -> None:
        self.status = spec.status
        self.headers = _StubHeaders(spec.headers)
        self.reason = spec.reason
        self.version = aiohttp.HttpVersion(1, 1)
        self.content = _StubContent(spec.body)
        self._closed = False

    def release(self) -> None:
        """Mark the stub released. Idempotent."""
        self._closed = True

    def close(self) -> None:
        """Mark the stub closed. Idempotent."""
        self._closed = True

    @property
    def closed(self) -> bool:
        """Whether `release` or `close` has been called."""
        return self._closed


class _RealServer:
    """Lazily-started ``aiohttp.web`` server backing real-network outcomes.

    One instance per harness (so per pytest-asyncio test, since a fresh
    harness is built per test): started on the first real exchange, on
    whichever event loop is current at that point, and left running for the
    rest of that test. Tracked in `_LIVE_SERVERS` so `reap_test_resources`
    can properly ``cleanup()`` it once the test is done.
    """

    def __init__(self, core: _HarnessCore) -> None:
        self._core = core
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> int:
        """Start the server on first use; return its loopback port."""
        async with self._lock:
            if self._runner is None:
                app = web.Application()
                app.router.add_route("*", "/{tail:.*}", self._handle)
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                sockets = site._server.sockets  # type: ignore[union-attr]
                assert sockets, "server has no listening sockets"
                self._port = int(sockets[0].getsockname()[1])
                self._runner = runner
                _LIVE_SERVERS.append(runner)
        assert self._port is not None
        return self._port

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """Serve one exchange by consuming the next programmed outcome."""
        outcome = self._core.take()
        await request.read()
        if outcome.kind is OutcomeKind.RESPONSE:
            assert outcome.response is not None
            return await _build_response(request, outcome.response)
        if outcome.kind is OutcomeKind.READ_TIMEOUT:
            await _hold_or_give_up()
            return web.Response(status=200)
        # PROTOCOL_ERROR / CANCELLED: sever the connection abruptly so the
        # client observes a genuine mid-exchange transport failure.
        transport = request.transport
        if transport is not None:
            transport.close()
        await _hold_or_give_up()
        return web.Response(status=200)


async def _hold_or_give_up() -> None:
    """Wait for the client to disconnect, bounded by `_SERVER_HOLD_SECONDS`."""
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.Event().wait(), timeout=_SERVER_HOLD_SECONDS)


async def _build_response(request: web.Request, spec: ResponseSpec) -> web.StreamResponse:
    """Serve ``spec`` as a real HTTP/1.1 response (buffered or streamed)."""
    status = spec.status
    assert isinstance(status, int)  # non-numeric statuses use the stub path
    headers = list(spec.headers)
    if not spec.stream:
        return web.Response(status=status, reason=spec.reason, headers=headers, body=spec.body)
    response = web.StreamResponse(status=status, reason=spec.reason, headers=headers)
    await response.prepare(request)
    for chunk in spec.chunks:
        await response.write(chunk)
    if spec.block_after_chunks:
        await _hold_or_give_up()
        return response
    await response.write_eof()
    return response


class _HarnessResolver(aiohttp.abc.AbstractResolver):
    """Routes every real connection attempt to the loopback server.

    ``CONNECT_ERROR`` / ``CONNECT_TIMEOUT`` never reach this resolver at all —
    the session-level override (`_install_request_override`) intercepts and
    synthesises them before a real connect is ever attempted.
    """

    def __init__(self, core: _HarnessCore, server: _RealServer) -> None:
        self._core = core
        self._server = server

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        real_port = await self._server.ensure_started()
        return [_resolve_result(host, "127.0.0.1", real_port)]

    async def close(self) -> None:
        """No resources of its own to release."""
        return None


#: Every real session and every real server this harness has built during the
#: current pytest test item, not yet reaped. See `reap_test_resources`.
_LIVE_SESSIONS: list[aiohttp.ClientSession] = []
_LIVE_SERVERS: list[web.AppRunner] = []


async def reap_test_resources() -> None:
    """Properly close every session and server this harness built for the
    just-finished test item.

    Every exchange this harness drives owns a throwaway `ClientSession` +
    `TCPConnector` + loopback `aiohttp.web` server, all scoped to one
    pytest-asyncio test (a fresh event loop per test). The shared battery's
    test bodies intentionally do not call `aclose_client` in most cases —
    that is not the facet under test there — so left to themselves these
    resources are only reachable by ordinary garbage collection, on a
    schedule this module does not control; aiohttp's ``ClientSession.__del__``
    and the *raw socket*'s own ``ResourceWarning`` (the listening/connected
    sockets `asyncio` tracks, independent of any aiohttp-level bookkeeping)
    both treat that as a leak, and this workspace's ``filterwarnings =
    ["error"]`` gate promotes either into a test failure.

    The underlying OS socket needs its owning event loop to process one more
    iteration to finish tearing down, so this must run as a real ``await`` on
    that same loop before it closes — a synchronous "mark closed" flag is not
    enough. `conftest.py`'s ``pytest_runtest_teardown`` hook (a *global*
    pytest hook, unlike a fixture, so it fires for every test regardless of
    which conftest.py defines it — including the shared battery's own test
    file) drives this via ``loop.run_until_complete`` immediately after each
    test item, while that item's own loop is still open but no longer
    running.
    """
    while _LIVE_SESSIONS:
        await _LIVE_SESSIONS.pop().close()
    while _LIVE_SERVERS:
        await _LIVE_SERVERS.pop().cleanup()


def _install_request_override(session: aiohttp.ClientSession, core: _HarnessCore) -> None:
    """Swap in an instance-level override of ``session.request``.

    ``ClientSession`` explicitly discourages subclassing (it warns on
    ``__init_subclass__``), so the interception seam is an instance attribute
    shadowing the bound method rather than a subclass override. The override
    captures exactly what the adapter handed to aiohttp (`SentRequest`) and,
    for the four facets described in the module docstring, raises or returns
    a synthetic native result instead of touching the network at all.
    """
    real_request = session.request

    async def _request(
        *,
        method: str,
        url: str,
        headers: Sequence[tuple[str, str]] | None = None,
        data: object = None,
        timeout: aiohttp.ClientTimeout | None = None,
        allow_redirects: bool = False,
    ) -> aiohttp.ClientResponse:
        body = await _drain(data)
        effective_timeout = timeout.sock_read if timeout is not None else None
        core.record_sent(
            SentRequest(
                method=method,
                url=url,
                headers=Headers(tuple(headers or ())),
                body=body,
                timeout=effective_timeout,
            )
        )
        kind = core.peek_kind()
        if kind is OutcomeKind.CONNECT_ERROR:
            core.take()
            raise _synthetic_connect_error()
        if kind is OutcomeKind.CONNECT_TIMEOUT:
            core.take()
            raise aiohttp.ConnectionTimeoutError(f"connect to {url} timed out (harness)")
        if kind is OutcomeKind.RESPONSE and _needs_stub(core.peek_response()):
            outcome = core.take()
            assert outcome.response is not None
            stub = _StubResponse(outcome.response)
            core.responses.append(stub)
            return stub  # type: ignore[return-value]
        response = await real_request(
            method=method,
            url=url,
            headers=headers,
            data=body,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        core.responses.append(response)
        return response

    session.request = _request  # type: ignore[assignment]


def _build_session(core: _HarnessCore, server: _RealServer) -> aiohttp.ClientSession:
    """Build a real, harness-wired `aiohttp.ClientSession`."""
    resolver = _HarnessResolver(core, server)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, force_close=True)
    session = aiohttp.ClientSession(connector=connector)
    _install_request_override(session, core)
    _LIVE_SESSIONS.append(session)
    return session


class AiohttpAsyncHarness(AsyncTransportHarness):
    """Real-``aiohttp``-backed harness for the async conformance battery.

    Capability flags reflect genuine transport limits, not laziness:

    - `supports_native_header_rejection` is ``False``: aiohttp has no
      per-header-name native rejection list for the adapter to consult
      (unlike the in-memory fake, which models one deliberately), so this
      facet is honestly unsupported rather than faked.
    - `supports_proxy_introspection` is ``False``: the adapter has no proxy
      configuration surface at all today.
    - `supports_byo` and `supports_streaming` are ``True``: both are real,
      exercised via a caller-supplied session and a real chunked HTTP
      response respectively.
    """

    supports_byo = True
    supports_native_header_rejection = False
    supports_streaming = True
    supports_proxy_introspection = False

    def __init__(self) -> None:
        self._core = _HarnessCore()
        self._server = _RealServer(self._core)
        self._byo_session: aiohttp.ClientSession | None = None

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
        """No-op for this transport.

        `supports_native_header_rejection` is ``False``, so the battery
        never relies on this call meaningfully.
        """
        return None

    def byo_underlying(self) -> ClosableNative | None:
        return self._byo_session

    def new_async_client(self, **options: object) -> AsyncHttpClient:
        timeout = options.get("timeout", _DEFAULT_CLIENT_TIMEOUT)
        assert isinstance(timeout, int | float)
        use_byo = bool(options.get("byo", False))
        session = _build_session(self._core, self._server)
        if use_byo:
            self._byo_session = session
        return AiohttpHttpClient(timeout=float(timeout), session=session)

    async def aclose_client(self, client: AsyncHttpClient) -> None:
        assert isinstance(client, AiohttpHttpClient)
        await client.aclose()


__all__ = ["AiohttpAsyncHarness"]
