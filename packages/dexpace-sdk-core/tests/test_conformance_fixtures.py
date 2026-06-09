# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Behavioral conformance fixtures — one observable check per seam (Q2).

Each test pins the *observable* contract of one behavioral seam — not its
private state — so a quiet regression in any of these load-bearing behaviors
fails the build. Where a seam only makes sense end-to-end, the check drives a
mock ``HttpClient`` / ``Pipeline`` rather than poking at internals:

* a single-use request body raises on a second read;
* ``RequestBody.to_replayable`` makes a single-use body retryable;
* ``Headers`` lookups are case-insensitive;
* the ``ContextStore`` evicts an entry on ``CallContext.close``;
* the retry policy honours a ``Retry-After`` header;
* the paginator iterates items across every page;
* the webhook verifier accepts a valid signature and rejects a tampered one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Headers, MediaType, Protocol, Url
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE
from dexpace.sdk.core.http.context import ContextStore, DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.http.webhooks import (
    InvalidWebhookSignatureError,
    WebhookVerifier,
)
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pagination import CursorStrategy, Paginator
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import RetryPolicy

from .conftest import FakeClock


def _instrumentation(trace_id_value: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace_id_value),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


# ----- single-use body raises on second read ------------------------------


def test_single_use_stream_body_raises_on_second_read() -> None:
    body = RequestBody.from_stream(io.BytesIO(b"payload"))
    assert b"".join(body.iter_bytes()) == b"payload"
    with pytest.raises(RuntimeError, match="already called"):
        list(body.iter_bytes())


def test_single_use_iter_body_raises_on_second_read() -> None:
    body = RequestBody.from_iter([b"a", b"b", b"c"])
    assert b"".join(body.iter_bytes()) == b"abc"
    with pytest.raises(RuntimeError, match="already called"):
        list(body.iter_bytes())


# ----- to_replayable makes a single-use body retryable --------------------


def test_to_replayable_allows_repeated_reads_of_a_stream_body() -> None:
    body = RequestBody.from_stream(io.BytesIO(b"retry-me")).to_replayable()
    assert body.is_replayable()
    # Two independent reads, modelling an initial send plus one retry.
    assert b"".join(body.iter_bytes()) == b"retry-me"
    assert b"".join(body.iter_bytes()) == b"retry-me"


def test_retry_replays_a_single_use_body_across_attempts() -> None:
    sent: list[bytes] = []

    class _DrainingClient(HttpClient):
        """Fails once (drains the body), then succeeds on the replayed body."""

        def __init__(self) -> None:
            self.attempts = 0

        def execute(self, request: Request) -> Response:
            self.attempts += 1
            assert request.body is not None
            sent.append(b"".join(request.body.iter_bytes()))
            status = Status.SERVICE_UNAVAILABLE if self.attempts == 1 else Status.OK
            return Response(request=request, protocol=Protocol.HTTP_1_1, status=status)

    client = _DrainingClient()
    request = Request(
        method=Method.PUT,
        url=Url.parse("https://api.example.com/things/1"),
        body=RequestBody.from_iter([b"single-use-payload"]),
    )
    retry = RetryPolicy(clock=FakeClock())
    with Pipeline(client, policies=[retry]) as pipeline:
        response = pipeline.run(request, DispatchContext(_instrumentation("0" * 16 + "a")))

    assert response.status is Status.OK
    assert client.attempts == 2
    # The retry replayed the exact same bytes the first attempt drained — the
    # observable proof that retry auto-buffered the single-use body.
    assert sent == [b"single-use-payload", b"single-use-payload"]


# ----- Headers case-insensitive lookup ------------------------------------


def test_headers_lookup_is_case_insensitive() -> None:
    headers = Headers({"Content-Type": "application/json"})
    assert headers.get("content-type") == "application/json"
    assert headers.get("CONTENT-TYPE") == "application/json"
    assert headers.get("Content-Type") == "application/json"
    assert "cOnTeNt-TyPe" in headers
    # A typed header-name constant resolves identically to its string form.
    assert headers.get(CONTENT_TYPE) == "application/json"


def test_headers_canonicalises_name_on_iteration() -> None:
    headers = Headers({"X-Custom-Header": "v"})
    assert tuple(headers) == ("x-custom-header",)


# ----- ContextStore evicts on close ---------------------------------------


def test_context_store_evicts_entry_on_close() -> None:
    trace_id = "0" * 16 + "b"
    instr = _instrumentation(trace_id)
    dispatch = DispatchContext(instrumentation_context=instr)

    request_ctx = dispatch.to_request_context(
        Request(method=Method.GET, url=Url.parse("https://example.com/"))
    )
    assert ContextStore.get(trace_id) is request_ctx

    request_ctx.close()
    assert ContextStore.get(trace_id) is None


def test_context_manager_exit_evicts_from_store() -> None:
    trace_id = "0" * 16 + "c"
    instr = _instrumentation(trace_id)
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(
        Request(method=Method.GET, url=Url.parse("https://example.com/"))
    )
    with request_ctx:
        assert ContextStore.get(trace_id) is request_ctx
    assert ContextStore.get(trace_id) is None


# ----- retry honours Retry-After ------------------------------------------


class _RetryAfterClient(HttpClient):
    """Returns 503 carrying a ``Retry-After`` header, then 200."""

    def __init__(self, retry_after: str) -> None:
        self._retry_after = retry_after
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        self.attempts += 1
        if self.attempts == 1:
            response = Response(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.SERVICE_UNAVAILABLE,
            )
            return response.with_header("Retry-After", self._retry_after)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def test_retry_sleeps_for_the_retry_after_delay() -> None:
    clock = FakeClock(start=1_000.0)
    client = _RetryAfterClient(retry_after="7")
    retry = RetryPolicy(clock=clock)
    request = Request(method=Method.GET, url=Url.parse("https://example.com/"))
    with Pipeline(client, policies=[retry]) as pipeline:
        response = pipeline.run(request, DispatchContext(_instrumentation("0" * 16 + "d")))

    assert response.status is Status.OK
    assert client.attempts == 2
    # The server asked for exactly 7 seconds; the policy slept precisely that
    # long (not the computed backoff).
    assert clock.monotonic() == pytest.approx(1_007.0)


def test_retry_caps_a_hostile_retry_after_header() -> None:
    clock = FakeClock(start=0.0)
    client = _RetryAfterClient(retry_after="999999")  # ~11.5 days
    retry = RetryPolicy(clock=clock, retry_after_max=30.0)
    request = Request(method=Method.GET, url=Url.parse("https://example.com/"))
    with Pipeline(client, policies=[retry]) as pipeline:
        pipeline.run(request, DispatchContext(_instrumentation("0" * 16 + "e")))

    # A multi-day header is clamped to the configured ceiling.
    assert clock.monotonic() == pytest.approx(30.0)


# ----- pagination iterates items across pages -----------------------------


class _InMemoryBody(ResponseBody):
    """Response body backed by an in-memory ``bytes`` buffer."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield self._data

    def close(self) -> None:
        self.closed = True


class _PagedClient(HttpClient):
    """Maps the ``cursor`` query value to a canned JSON page body."""

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        cursor = request.url.query.get("cursor")
        payload = self._pages[cursor]
        body = _InMemoryBody(json.dumps(payload).encode("utf-8"))
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
            body=body,
        )


def test_paginator_iterates_items_across_all_pages() -> None:
    client = _PagedClient(
        {
            None: {"data": [1, 2], "next_cursor": "c1"},
            "c1": {"data": [3, 4], "next_cursor": "c2"},
            "c2": {"data": [5], "next_cursor": None},
        }
    )
    strategy: CursorStrategy[int] = CursorStrategy(
        items_field="data",
        cursor_response_field="next_cursor",
        cursor_param="cursor",
    )
    first = Request(method=Method.GET, url=Url.parse("https://api.example.com/items"))
    paginator: Paginator[int] = Paginator(client.execute, strategy, first)

    assert list(paginator) == [1, 2, 3, 4, 5]
    # One transport call per page; the cursor was threaded onto each follow-up.
    assert len(client.calls) == 3
    assert client.calls[1].url.query.get("cursor") == "c1"
    assert client.calls[2].url.query.get("cursor") == "c2"


# ----- webhook verifier accepts valid / rejects tampered ------------------

_WEBHOOK_RAW_KEY = b"conformance-webhook-signing-key-0"
_WEBHOOK_SECRET = "whsec_" + base64.b64encode(_WEBHOOK_RAW_KEY).decode("ascii")
_WEBHOOK_ID = "msg_conformance_0001"
_WEBHOOK_TIMESTAMP = "1700000000"
_WEBHOOK_BODY = b'{"event":"order.created","id":"ord_42"}'


def _sign_webhook(body: bytes) -> str:
    content = f"{_WEBHOOK_ID}.{_WEBHOOK_TIMESTAMP}.".encode() + body
    digest = hmac.new(_WEBHOOK_RAW_KEY, content, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _webhook_headers(signature: str) -> dict[str, str]:
    return {
        "webhook-id": _WEBHOOK_ID,
        "webhook-timestamp": _WEBHOOK_TIMESTAMP,
        "webhook-signature": f"v1,{signature}",
    }


def _webhook_verifier() -> WebhookVerifier:
    return WebhookVerifier(_WEBHOOK_SECRET, clock=FakeClock(start=float(_WEBHOOK_TIMESTAMP)))


def test_webhook_verifier_accepts_a_valid_signature() -> None:
    headers = _webhook_headers(_sign_webhook(_WEBHOOK_BODY))
    payload = _webhook_verifier().unwrap(headers, _WEBHOOK_BODY)
    assert payload == {"event": "order.created", "id": "ord_42"}


def test_webhook_verifier_rejects_a_tampered_signature() -> None:
    signature = _sign_webhook(_WEBHOOK_BODY)
    tampered_body = _WEBHOOK_BODY.replace(b"ord_42", b"ord_99")
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _webhook_verifier().verify(_webhook_headers(signature), tampered_body)
