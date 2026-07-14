# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Transport-neutral seam for the transport-adapter conformance battery.

This module is the reusable contract layer. It defines *what* a transport
adapter must be programmed to do (``Outcome`` / ``ResponseSpec``), *what* the
battery observes about an exchange (``SentRequest``, ``DropLog``), and the two
harness ABCs (`SyncTransportHarness` / `AsyncTransportHarness`) that the battery
drives. It deliberately knows nothing about any concrete transport.

A harness is the single seam between a test body and a concrete adapter:

- the battery programs the next exchange with ``expect_*`` / ``mark_*`` methods,
- builds an adapter with ``new_client`` (or ``new_async_client``),
- and inspects what the adapter did through ``sent_requests`` /
  ``attempt_count`` / ``drop_log`` / ``native_responses`` / ``byo_*``.

Because every test body speaks only this seam, a transport adapter is certified
by shipping its own harness implementation and appending it to the registry — no
test body changes. This module and the ``test_*`` batteries ship together inside
the installed ``dexpace.sdk.tck`` package, and each transport-adapter package
supplies its own harness and registers it from that package's own
``tests/conftest.py``.

Capability flags let a harness declare which contract facets it can simulate;
the battery skips the facets a given transport cannot exercise rather than
forcing every harness to model every native quirk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
    from dexpace.sdk.core.client.http_client import HttpClient
    from dexpace.sdk.core.http.common.headers import Headers


class OutcomeKind(Enum):
    """The way a programmed native exchange resolves.

    The battery expresses transport behaviour in these neutral terms; each
    harness maps them onto its transport's native mechanism (a raised native
    exception, a mock response, a real socket close, ...).
    """

    RESPONSE = auto()
    CONNECT_ERROR = auto()
    CONNECT_TIMEOUT = auto()
    READ_TIMEOUT = auto()
    CANCELLED = auto()
    PROTOCOL_ERROR = auto()


@dataclass(frozen=True)
class ResponseSpec:
    """A native response the harness should hand back for one exchange.

    Attributes:
        status: The status code to surface. A non-numeric string models a
            response the adapter cannot adapt (adaptation must fail cleanly and
            close the native response).
        headers: Inbound header lines, verbatim — including ones the adapter
            must defensively drop (a malformed name) or preserve (obs-text).
        body: The buffered response payload (ignored when ``stream`` is set).
        reason: The reason phrase, if any.
        protocol: The wire protocol token (``http/1.1``, ``http/2``, ...).
        stream: When true, the body is produced lazily by a native producer so
            the battery can prove close-cascade and early-abandon behaviour.
        chunks: The lazily produced chunks (used only when ``stream`` is set).
        block_after_chunks: When true, the streaming producer blocks after
            yielding ``chunks`` instead of ending — used to prove that an
            abandoned or cancelled subscription unblocks the producer.
    """

    status: int | str = 200
    headers: Sequence[tuple[str, str]] = ()
    body: bytes = b""
    reason: str | None = None
    protocol: str = "http/1.1"
    stream: bool = False
    chunks: Sequence[bytes] = ()
    block_after_chunks: bool = False


@dataclass(frozen=True)
class Outcome:
    """A programmed result for a single native exchange."""

    kind: OutcomeKind
    response: ResponseSpec | None = None

    @classmethod
    def of_response(cls, spec: ResponseSpec) -> Outcome:
        """Wrap a `ResponseSpec` as a RESPONSE outcome."""
        return cls(OutcomeKind.RESPONSE, spec)


@dataclass
class SentRequest:
    """What actually reached the native layer after the adapter prepared it.

    Captures the post-adaptation view: content-type resolved, framing headers
    computed, native-rejected headers dropped. The battery asserts against this
    record, never against the caller's original `Request`.

    Attributes:
        method: The wire method token.
        url: The wire-form URL.
        headers: The headers the adapter handed to the native layer.
        body: The bytes the adapter wrote to the wire.
        timeout: The effective per-call timeout the adapter applied.
    """

    method: str
    url: str
    headers: Headers
    body: bytes
    timeout: float | None

    @property
    def content_type(self) -> str | None:
        """The content-type actually sent, or ``None``."""
        return self.headers.get("content-type")

    @property
    def content_length(self) -> str | None:
        """The content-length actually sent, or ``None``."""
        return self.headers.get("content-length")

    @property
    def transfer_encoding(self) -> str | None:
        """The transfer-encoding actually sent, or ``None``."""
        return self.headers.get("transfer-encoding")


class DropLog:
    """Bounded, case-insensitive record of headers the adapter dropped.

    Models the observability contract for a header the model layer accepts but
    the native library rejects: the drop is counted (never silent), the count
    is case-insensitive (``X-Bad`` and ``x-bad`` are one entry), and the number
    of *distinct* dropped names is bounded so a hostile stream of unique bad
    names cannot grow the log without limit.
    """

    __slots__ = ("_cap", "_counts")

    def __init__(self, cap: int = 64) -> None:
        self._cap = cap
        self._counts: dict[str, int] = {}

    def record(self, name: str) -> None:
        """Record one drop of ``name`` (case-insensitive, bounded by capacity)."""
        key = name.lower()
        if key in self._counts:
            self._counts[key] += 1
        elif len(self._counts) < self._cap:
            self._counts[key] = 1

    def count(self, name: str) -> int:
        """Return how many times ``name`` was dropped (case-insensitive)."""
        return self._counts.get(name.lower(), 0)

    @property
    def total(self) -> int:
        """Total drops recorded across all names."""
        return sum(self._counts.values())

    @property
    def distinct(self) -> int:
        """Number of distinct dropped names currently held."""
        return len(self._counts)

    @property
    def capacity(self) -> int:
        """The ceiling on distinct dropped names."""
        return self._cap


@runtime_checkable
class ClosableNative(Protocol):
    """A native response handle whose release the battery can observe."""

    @property
    def closed(self) -> bool:
        """Whether the underlying native response has been closed."""
        ...


class _BaseHarness(ABC):
    """Shared programming and introspection seam for both pillars.

    Capability flags default to ``True`` for the in-memory reference fake, which
    can simulate every facet. A real-adapter harness lowers the flags it cannot
    model; the battery skips the corresponding tests instead of failing them.
    """

    #: Whether the transport accepts a caller-supplied (BYO) native client.
    supports_byo: bool = True
    #: Whether the harness can inject a header the native library rejects.
    supports_native_header_rejection: bool = True
    #: Whether the harness can produce a lazily-streamed response body.
    supports_streaming: bool = True
    #: Whether the adapter exposes proxy capability introspection.
    supports_proxy_introspection: bool = True
    #: Whether the harness can program a native cooperative-cancellation
    #: outcome. The SDK's `HttpClient.execute(request)` seam has no
    #: cancellation hook of its own (`Request` carries no cancellation
    #: token) — cooperative cancellation is a pipeline/retry-loop concept in
    #: this SDK, not a transport one. A harness backed by a real, minimal
    #: transport with no cancellation channel of its own sets this `False`
    #: rather than forcing a misleading `True`.
    supports_cancellation: bool = True
    #: Whether the adapter can frame an unknown-upfront-length request body
    #: as genuine wire-level ``Transfer-Encoding: chunked`` rather than fully
    #: buffering it first. A transport that always buffers the whole body
    #: before send (documented as a "no streaming uploads" limitation) always
    #: knows the exact length once buffering completes, so it correctly uses
    #: ``Content-Length`` even for a body whose declared length was unknown
    #: upfront — it never emits (and should not fake) chunked framing.
    supports_unknown_length_chunked_framing: bool = True
    #: Whether the harness can produce a native response that is obtained
    #: successfully but then fails SDK-level adaptation (the fake models this
    #: with a non-numeric status token). A transport whose native library
    #: validates the status line itself (rejecting a non-numeric or
    #: out-of-range token before handing back any response object at all) —
    #: combined with the SDK's own `Status` being total over integers
    #: (TRANSPORT-24: no valid status ever fails adaptation) — can have no
    #: state where a native response is obtained yet adaptation fails, and
    #: sets this `False` rather than forcing a misleading `True`.
    supports_adaptation_failure: bool = True
    #: Whether the adapter can recover a good header that arrives *after* a
    #: malformed one in the same response. Every adapter in this SDK drops a
    #: single malformed inbound header without aborting the conversion, but a
    #: native library whose own header parser treats an invalid name as the
    #: end of the header block (silently discarding every header that
    #: follows) can lose "good" headers upstream of anything the adapter's
    #: own conversion logic controls. Set this `False` rather than forcing a
    #: misleading `True` when that upstream loss is real and verified, not
    #: assumed.
    supports_defensive_inbound_headers: bool = True
    #: Smallest positive timeout the adapter will apply (sub-resolution values
    #: clamp up to this rather than truncating to zero).
    min_timeout_granularity: float = 0.001

    @abstractmethod
    def expect(self, outcome: Outcome) -> None:
        """Program the next native exchange to resolve as ``outcome``."""

    @abstractmethod
    def sent_requests(self) -> list[SentRequest]:
        """Return, in order, every request the adapter handed to the native layer."""

    @abstractmethod
    def attempt_count(self) -> int:
        """Return how many native exchanges the adapter initiated."""

    @abstractmethod
    def drop_log(self) -> DropLog:
        """Return the log of headers the adapter dropped as native-rejected."""

    @abstractmethod
    def native_responses(self) -> list[ClosableNative]:
        """Return the native response handles produced so far (for close checks)."""

    @abstractmethod
    def mark_native_rejected(self, *names: str) -> None:
        """Declare header names the native library rejects (adapter must drop them)."""

    @abstractmethod
    def byo_underlying(self) -> ClosableNative | None:
        """Return the caller-supplied native client, if the adapter was built with one."""

    def unsupported_proxy_features(self, client: object) -> frozenset[str]:
        """Return proxy features the built ``client`` cannot honour.

        Only meaningful when ``supports_proxy_introspection`` is set; the base
        implementation raises so a harness that advertises the capability must
        override it.
        """
        raise NotImplementedError

    def shutdown(self) -> None:  # noqa: B027  # intentional no-op default, not abstract
        """Release any process-level resources the harness holds. Idempotent.

        The battery's fixtures call this in teardown. A harness backed by a
        real server/socket (rather than the in-memory fake) overrides it to
        tear that down; the default is intentionally a no-op — an in-memory
        harness holds nothing to release, so this is a real (empty) default,
        not an unimplemented abstract method.
        """

    # ----- Convenience programming (implemented on the neutral seam) --------

    def expect_response(
        self,
        status: int | str = 200,
        *,
        headers: Sequence[tuple[str, str]] = (),
        body: bytes = b"",
        reason: str | None = None,
        protocol: str = "http/1.1",
        stream: bool = False,
        chunks: Sequence[bytes] = (),
        block_after_chunks: bool = False,
    ) -> None:
        """Program a native response for the next exchange."""
        self.expect(
            Outcome.of_response(
                ResponseSpec(
                    status=status,
                    headers=tuple(headers),
                    body=body,
                    reason=reason,
                    protocol=protocol,
                    stream=stream,
                    chunks=tuple(chunks),
                    block_after_chunks=block_after_chunks,
                )
            )
        )

    def expect_connect_error(self) -> None:
        """Program a connection-level failure that never yields a response."""
        self.expect(Outcome(OutcomeKind.CONNECT_ERROR))

    def expect_connect_timeout(self) -> None:
        """Program a connect-phase timeout."""
        self.expect(Outcome(OutcomeKind.CONNECT_TIMEOUT))

    def expect_read_timeout(self) -> None:
        """Program a read-phase timeout."""
        self.expect(Outcome(OutcomeKind.READ_TIMEOUT))

    def expect_cancellation(self) -> None:
        """Program a cooperative cancellation of the in-flight exchange."""
        self.expect(Outcome(OutcomeKind.CANCELLED))

    def expect_protocol_error(self) -> None:
        """Program a generic transport error mid-exchange."""
        self.expect(Outcome(OutcomeKind.PROTOCOL_ERROR))


class SyncTransportHarness(_BaseHarness):
    """Builds and programs a synchronous transport adapter under test."""

    @abstractmethod
    def new_client(self, **options: object) -> HttpClient:
        """Return a fresh synchronous adapter bound to this harness's program.

        Recognised options (a real-adapter harness may ignore any it cannot
        honour): ``timeout`` (float), ``byo`` (bool — build over a
        caller-supplied underlying client), ``proxy_url`` (str),
        ``proxy_features`` (frozenset[str]).
        """

    @abstractmethod
    def close_client(self, client: HttpClient) -> None:
        """Close ``client`` through whatever close surface the adapter exposes."""


class AsyncTransportHarness(_BaseHarness):
    """Builds and programs an asynchronous transport adapter under test."""

    @abstractmethod
    def new_async_client(self, **options: object) -> AsyncHttpClient:
        """Return a fresh asynchronous adapter bound to this harness's program.

        See `SyncTransportHarness.new_client` for the recognised options.
        """

    @abstractmethod
    async def aclose_client(self, client: AsyncHttpClient) -> None:
        """Close ``client`` through whatever async close surface the adapter exposes."""


# Registries each transport-adapter package appends its harness factory to.
# Populated for the in-memory reference fake in this package's ``conftest.py``;
# each real adapter appends its factory from its own package's ``tests/conftest.py``.
SYNC_HARNESS_FACTORIES: list[tuple[str, type[SyncTransportHarness]]] = []
ASYNC_HARNESS_FACTORIES: list[tuple[str, type[AsyncTransportHarness]]] = []


__all__ = [
    "ASYNC_HARNESS_FACTORIES",
    "SYNC_HARNESS_FACTORIES",
    "AsyncTransportHarness",
    "ClosableNative",
    "DropLog",
    "Outcome",
    "OutcomeKind",
    "ResponseSpec",
    "SentRequest",
    "SyncTransportHarness",
]
