# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous transport-adapter conformance battery.

Every test drives a ``sync_harness`` — the parametrized seam — and never a
concrete transport. The suite is proven against the in-memory reference fake;
a certification task points it at a real adapter by registering that adapter's
harness (see ``conftest.py``), with no change to any test body below.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from dexpace.sdk.core.errors import (
    NetworkError,
    RequestCancelledError,
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Response, Status

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .harness import SyncTransportHarness

_URL = "http://service.test/v1"


def _get(url: str = _URL, *, timeout: float | None = None) -> Request:
    return Request(method=Method.GET, url=Url.parse(url), timeout=timeout)


def _post(body: RequestBody, url: str = "http://service.test/upload") -> Request:
    return Request(method=Method.POST, url=Url.parse(url), body=body)


def _classify(err: BaseException) -> str:
    """Discriminate an error by TYPE, timeout before cancellation.

    Both timeout types and ``RequestCancelledError`` are ``OSError`` subtypes,
    so the timeout check must come first or a timeout would be misread as a
    cancellation (or a generic network error). This mirrors the ordering a
    retry policy must use.
    """
    if isinstance(err, ServiceRequestTimeoutError | ServiceResponseTimeoutError):
        return "timeout"
    if isinstance(err, RequestCancelledError):
        return "cancelled"
    if isinstance(err, NetworkError):
        return "network"
    return "other"


def _execute_error(harness: SyncTransportHarness, request: Request) -> BaseException:
    """Execute ``request`` expecting a failure; return the raised error."""
    client = harness.new_client()
    with pytest.raises(SdkError) as excinfo:
        client.execute(request)
    return excinfo.value


# --------------------------------------------------------------------------- #
# Success shape + redirect/retry inertia.
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-11")
def test_success_yields_a_response_never_none(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200, body=b"ok")
    client = sync_harness.new_client()
    response = client.execute(_get())
    assert response is not None
    assert isinstance(response, Response)
    with response as resp:
        assert resp.body is not None
        assert resp.body.bytes() == b"ok"


@pytest.mark.req("TRANSPORT-1")
def test_adapter_does_not_follow_redirects(sync_harness: SyncTransportHarness) -> None:
    # A 3xx surfaces as an ordinary response; the SDK redirect policy — not the
    # adapter — owns following it. Exactly one exchange reaches the transport.
    sync_harness.expect_response(302, headers=[("Location", "http://service.test/moved")])
    client = sync_harness.new_client()
    with client.execute(_get()) as response:
        assert response.status == Status(302)
        assert response.headers.get("location") == "http://service.test/moved"
    assert sync_harness.attempt_count() == 1


@pytest.mark.req("TRANSPORT-2")
def test_adapter_does_not_retry_on_failure(sync_harness: SyncTransportHarness) -> None:
    # A native failure is mapped and raised once; retry is a pipeline policy,
    # never adapter magic. Only one exchange is attempted.
    sync_harness.expect_connect_error()
    _execute_error(sync_harness, _get())
    assert sync_harness.attempt_count() == 1


# --------------------------------------------------------------------------- #
# Error mapping.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-20")
def test_connection_failure_maps_to_retryable_network_error(
    sync_harness: SyncTransportHarness,
) -> None:
    sync_harness.expect_connect_error()
    err = _execute_error(sync_harness, _get())
    assert isinstance(err, ServiceRequestError)
    assert isinstance(err, NetworkError)
    assert err.retryable is True


@pytest.mark.req("TRANSPORT-20")
def test_connect_timeout_maps_to_retryable_timeout_type(
    sync_harness: SyncTransportHarness,
) -> None:
    sync_harness.expect_connect_timeout()
    err = _execute_error(sync_harness, _get())
    assert isinstance(err, ServiceRequestTimeoutError)
    assert isinstance(err, NetworkError)
    assert err.retryable is True
    assert not isinstance(err, RequestCancelledError)
    assert _classify(err) == "timeout"


def test_read_timeout_maps_to_response_timeout_type(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_read_timeout()
    err = _execute_error(sync_harness, _get())
    assert isinstance(err, ServiceResponseTimeoutError)
    assert not isinstance(err, RequestCancelledError)
    assert _classify(err) == "timeout"


@pytest.mark.req("TRANSPORT-3")
def test_cancellation_maps_to_terminal_cancelled(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_cancellation:
        pytest.skip("harness cannot program a native cooperative-cancellation outcome")
    sync_harness.expect_cancellation()
    err = _execute_error(sync_harness, _get())
    assert isinstance(err, RequestCancelledError)
    assert isinstance(err, OSError)
    assert not isinstance(err, NetworkError)
    assert not isinstance(err, ServiceRequestTimeoutError | ServiceResponseTimeoutError)
    assert err.retryable is False
    assert _classify(err) == "cancelled"


@pytest.mark.req("TRANSPORT-3")
def test_timeout_is_discriminated_before_cancellation(sync_harness: SyncTransportHarness) -> None:
    # Both a timeout and a cancellation are OSErrors, yet the type-ordered
    # classifier keeps them apart: timeout -> retryable, cancellation -> terminal.
    if not sync_harness.supports_cancellation:
        pytest.skip("harness cannot program a native cooperative-cancellation outcome")
    sync_harness.expect_connect_timeout()
    timeout_err = _execute_error(sync_harness, _get())
    sync_harness.expect_cancellation()
    cancel_err = _execute_error(sync_harness, _get())
    assert isinstance(timeout_err, OSError) and isinstance(cancel_err, OSError)
    assert _classify(timeout_err) == "timeout"
    assert _classify(cancel_err) == "cancelled"


def test_protocol_error_maps_to_request_error(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_protocol_error()
    err = _execute_error(sync_harness, _get())
    assert isinstance(err, ServiceRequestError)


# --------------------------------------------------------------------------- #
# Content-Type resolution + adapter-computed framing.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-10")
def test_explicit_content_type_is_authoritative(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    request = _post(body).with_header("Content-Type", "text/plain")
    sync_harness.new_client().execute(request).close()
    assert sync_harness.sent_requests()[0].content_type == "text/plain"


@pytest.mark.req("TRANSPORT-10")
def test_body_derived_content_type_used_when_absent(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    sync_harness.new_client().execute(_post(body)).close()
    assert sync_harness.sent_requests()[0].content_type == "application/json"


@pytest.mark.req("TRANSPORT-11")
def test_content_length_is_computed_by_adapter(sync_harness: SyncTransportHarness) -> None:
    # A caller-set Content-Length that disagrees with the real body is not
    # trusted: the adapter recomputes it from the bytes it actually writes.
    sync_harness.expect_response(200)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://service.test/upload"),
        headers=Headers([("Content-Length", "999")]),
        body=RequestBody.from_bytes(b"hello world"),
    )
    sync_harness.new_client().execute(request).close()
    sent = sync_harness.sent_requests()[0]
    assert sent.content_length == "11"
    assert sent.transfer_encoding is None


def test_unknown_length_body_is_chunked_without_content_length(
    sync_harness: SyncTransportHarness,
) -> None:
    if not sync_harness.supports_unknown_length_chunked_framing:
        pytest.skip("harness always buffers the body fully before send")
    sync_harness.expect_response(200)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://service.test/upload"),
        headers=Headers([("Content-Length", "5")]),
        body=RequestBody.from_iter(iter([b"a", b"bc"])),
    )
    sync_harness.new_client().execute(request).close()
    sent = sync_harness.sent_requests()[0]
    assert sent.content_length is None
    assert sent.transfer_encoding == "chunked"


@pytest.mark.req("TRANSPORT-26")
def test_empty_body_bearing_method_sends_zero_length(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    request = Request(method=Method.POST, url=Url.parse("http://service.test/x"))
    sync_harness.new_client().execute(request).close()
    assert sync_harness.sent_requests()[0].content_length == "0"


@pytest.mark.req("TRANSPORT-26")
def test_bodyless_get_has_no_content_length(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    sync_harness.new_client().execute(_get()).close()
    assert sync_harness.sent_requests()[0].content_length is None


# --------------------------------------------------------------------------- #
# Header hygiene: native-rejected outbound + defensive inbound.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-12", "TRANSPORT-13")
def test_native_rejected_header_dropped_counted_and_logged(
    sync_harness: SyncTransportHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if not sync_harness.supports_native_header_rejection:
        pytest.skip("harness cannot inject a native-rejected header")
    sync_harness.mark_native_rejected("X-Bad")
    sync_harness.expect_response(200)
    request = _get().with_header("X-Bad", "boom").with_header("X-Ok", "fine")
    with caplog.at_level(logging.WARNING):
        sync_harness.new_client().execute(request).close()
    sent = sync_harness.sent_requests()[0]
    assert sent.headers.get("x-bad") is None
    assert sent.headers.get("x-ok") == "fine"
    # Counted case-insensitively; the drop is observable in the log.
    assert sync_harness.drop_log().count("X-BAD") == 1
    assert sync_harness.drop_log().count("x-bad") == 1
    assert any("native-rejected" in message for message in caplog.messages)


@pytest.mark.req("TRANSPORT-13")
def test_native_rejected_drop_log_is_bounded(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_native_header_rejection:
        pytest.skip("harness cannot inject a native-rejected header")
    drop_log = sync_harness.drop_log()
    names = [f"X-Bad-{i}" for i in range(drop_log.capacity + 20)]
    sync_harness.mark_native_rejected(*names)
    sync_harness.expect_response(200)
    request = _get()
    for name in names:
        request = request.with_header(name, "x")
    sync_harness.new_client().execute(request).close()
    assert drop_log.distinct <= drop_log.capacity


@pytest.mark.req("TRANSPORT-14")
def test_malformed_inbound_header_dropped_obs_text_preserved(
    sync_harness: SyncTransportHarness,
) -> None:
    if not sync_harness.supports_defensive_inbound_headers:
        pytest.skip("harness cannot recover a good header following a malformed one")
    sync_harness.expect_response(
        200,
        headers=[("X-Good", "ok"), ("Bad Name", "x"), ("X-Obs", "caf\xe9")],
    )
    with sync_harness.new_client().execute(_get()) as response:
        assert set(response.headers.names()) == {"x-good", "x-obs"}
        assert response.headers.get("x-obs") == "caf\xe9"


@pytest.mark.req("TRANSPORT-27")
def test_malformed_inbound_content_length_downgraded(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200, headers=[("Content-Length", "not-a-number")], body=b"data")
    with sync_harness.new_client().execute(_get()) as response:
        assert response.body is not None
        assert response.body.content_length() == -1
        assert response.body.bytes() == b"data"


@pytest.mark.req("TRANSPORT-27")
def test_malformed_inbound_content_type_downgraded(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200, headers=[("Content-Type", "///bad")], body=b"data")
    with sync_harness.new_client().execute(_get()) as response:
        assert response.body is not None
        assert response.body.media_type() is None


# --------------------------------------------------------------------------- #
# Request body semantics.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-17")
def test_single_use_body_written_exactly_once(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    body = RequestBody.from_iter(iter([b"chunk-1;", b"chunk-2"]))
    sync_harness.new_client().execute(_post(body)).close()
    assert sync_harness.sent_requests()[0].body == b"chunk-1;chunk-2"
    assert sync_harness.attempt_count() == 1
    with pytest.raises(RuntimeError):
        list(body.iter_bytes())


@pytest.mark.req("TRANSPORT-17", "TRANSPORT-18")
def test_replayable_body_replays_byte_identical(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    sync_harness.expect_response(200)
    body = RequestBody.from_bytes(b"payload-bytes")
    client = sync_harness.new_client()
    client.execute(_post(body)).close()
    client.execute(_post(body)).close()
    sent = sync_harness.sent_requests()
    assert sent[0].body == sent[1].body == b"payload-bytes"


@pytest.mark.req("TRANSPORT-28")
def test_file_backed_body_is_replayable(sync_harness: SyncTransportHarness, tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"file-payload")
    sync_harness.expect_response(200)
    sync_harness.expect_response(200)
    body = RequestBody.from_file(path)
    client = sync_harness.new_client()
    client.execute(_post(body)).close()
    client.execute(_post(body)).close()
    sent = sync_harness.sent_requests()
    assert sent[0].body == sent[1].body == b"file-payload"


@pytest.mark.req("TRANSPORT-18")
def test_replayable_buffering_failure_is_clean(sync_harness: SyncTransportHarness) -> None:
    # A body-producer whose buffering fails must fail loudly, not send a
    # truncated payload or a silent empty body.
    sync_harness.expect_response(200)
    body = _FailingBody()
    with pytest.raises(OSError, match="disk gone"):
        sync_harness.new_client().execute(_post(body))
    assert sync_harness.attempt_count() == 0
    assert sync_harness.sent_requests() == []


# --------------------------------------------------------------------------- #
# Response lifecycle: adaptation failure + streaming close cascade.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-22")
def test_adaptation_failure_closes_native_response(sync_harness: SyncTransportHarness) -> None:
    # A non-numeric status models a native response the adapter cannot adapt.
    if not sync_harness.supports_adaptation_failure:
        pytest.skip("harness cannot obtain a native response that then fails adaptation")
    sync_harness.expect_response("BAD")
    with pytest.raises(ServiceResponseError):
        sync_harness.new_client().execute(_get())
    assert sync_harness.native_responses()[-1].closed is True


@pytest.mark.req("SEAM-11", "TRANSPORT-25")
def test_streamed_response_close_cascades_to_native(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_streaming:
        pytest.skip("harness cannot produce a streamed response body")
    sync_harness.expect_response(200, stream=True, chunks=[b"a", b"b", b"c"])
    response = sync_harness.new_client().execute(_get())
    response.close()
    assert sync_harness.native_responses()[-1].closed is True


@pytest.mark.req("TRANSPORT-25")
def test_abandoned_stream_releases_producer(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_streaming:
        pytest.skip("harness cannot produce a streamed response body")
    sync_harness.expect_response(200, stream=True, chunks=[b"one", b"two", b"three"])
    response = sync_harness.new_client().execute(_get())
    assert response.body is not None
    first = next(response.body.iter_bytes(3))
    assert first  # consumed one chunk, then abandon the subscription
    response.close()
    assert sync_harness.native_responses()[-1].closed is True


# --------------------------------------------------------------------------- #
# Timeout handling + concurrency.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-6")
def test_sub_resolution_timeout_clamped_up(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    sync_harness.new_client().execute(_get(timeout=0.0001)).close()
    sent = sync_harness.sent_requests()[0]
    assert sent.timeout is not None
    assert sent.timeout >= sync_harness.min_timeout_granularity
    assert sent.timeout > 0


@pytest.mark.req("SEAM-12", "TRANSPORT-29", "TRANSPORT-5")
def test_per_call_timeout_isolation(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(200)
    sync_harness.expect_response(200)
    client = sync_harness.new_client()

    def run(timeout: float) -> None:
        client.execute(_get(timeout=timeout)).close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, 0.5), pool.submit(run, 5.0)]
        for future in futures:
            future.result()
    assert {sent.timeout for sent in sync_harness.sent_requests()} == {0.5, 5.0}


@pytest.mark.req("SEAM-12", "TRANSPORT-29")
def test_concurrent_execute_is_safe(sync_harness: SyncTransportHarness) -> None:
    count = 16
    for _ in range(count):
        sync_harness.expect_response(200, body=b"ok")
    client = sync_harness.new_client()

    def run() -> int:
        with client.execute(_get()) as response:
            assert response.body is not None
            response.body.bytes()
            return int(response.status)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: run(), range(count)))
    assert results == [200] * count
    assert sync_harness.attempt_count() == count


# --------------------------------------------------------------------------- #
# Status totality (already-shipped TRANSPORT-24 behaviour).
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-24")
def test_out_of_range_status_surfaces_with_body(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(999, body=b"vendor code")
    with sync_harness.new_client().execute(_get()) as response:
        assert int(response.status) == 999
        assert response.body is not None
        assert response.body.bytes() == b"vendor code"


def test_in_range_unregistered_status_surfaces(sync_harness: SyncTransportHarness) -> None:
    sync_harness.expect_response(218, body=b"this is fine")
    with sync_harness.new_client().execute(_get()) as response:
        assert int(response.status) == 218
        assert response.body is not None
        assert response.body.bytes() == b"this is fine"


# --------------------------------------------------------------------------- #
# Ownership-aware close.
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-14", "TRANSPORT-15")
def test_byo_client_survives_adapter_close(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_byo:
        pytest.skip("harness cannot build over a caller-supplied client")
    sync_harness.expect_response(200)
    client = sync_harness.new_client(byo=True)
    client.execute(_get()).close()
    sync_harness.close_client(client)
    byo = sync_harness.byo_underlying()
    assert byo is not None
    assert byo.closed is False
    sync_harness.close_client(client)  # idempotent, non-blocking


@pytest.mark.req("SEAM-15")
def test_execute_after_close_raises(sync_harness: SyncTransportHarness) -> None:
    client = sync_harness.new_client()
    sync_harness.close_client(client)
    with pytest.raises(ServiceRequestError):
        client.execute(_get())


# --------------------------------------------------------------------------- #
# Proxy discoverability + credential safety.
# --------------------------------------------------------------------------- #


@pytest.mark.req("TRANSPORT-30")
def test_proxy_credentials_never_leak(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_proxy_introspection:
        pytest.skip("harness has no proxy surface")
    client = sync_harness.new_client(proxy_url="http://user:s3cret@proxy.test:8080")
    assert "s3cret" not in repr(client)


@pytest.mark.req("TRANSPORT-30")
def test_unsupported_proxy_feature_is_discoverable(sync_harness: SyncTransportHarness) -> None:
    if not sync_harness.supports_proxy_introspection:
        pytest.skip("harness has no proxy surface")
    client = sync_harness.new_client(
        proxy_url="socks5://user:s3cret@proxy.test:1080",
        proxy_features=frozenset({"socks"}),
    )
    unsupported = sync_harness.unsupported_proxy_features(client)
    assert "scheme:socks5" in unsupported


class _FailingBody(RequestBody):
    """A request body whose buffering fails partway, to prove clean failure."""

    def media_type(self) -> MediaType | None:
        return None

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield b"partial"
        raise OSError("disk gone")
