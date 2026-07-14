# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the response-handler family.

Two axes are exercised separately, matching the design split:

- the pure `classify_status` decision — a status band maps to a
  `ResponseDisposition` with no side effects (no reading, closing, or raising);
  and
- the `DecodeBody` / `DecodeSuccess` orchestration — reading and decoding the
  body, closing the response on every path, and raising the right typed error
  for non-success responses.

Imports come straight from `dexpace.sdk.core.serde.response_handler` so the
suite does not depend on the package re-export landing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from dexpace.sdk.core.codegen import StatusErrorMap
from dexpace.sdk.core.errors import (
    ERROR_BODY_MAX_BYTES,
    DeserializationError,
    HttpResponseError,
    SerdeError,
)
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status
from dexpace.sdk.core.serde.response_handler import (
    DecodeBody,
    DecodeSuccess,
    ResponseDisposition,
    ResponseHandler,
    classify_status,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Pet:
    name: str
    age: int


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def _response(status: Status, *, body: ResponseBody | None = None) -> Response:
    return Response(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)


def _tracking_response(
    status: Status, *, body: ResponseBody | None = None
) -> tuple[Response, list[int]]:
    """Return a response whose ``close`` appends to the returned list.

    Lets a test observe that the handler closed the response even on the
    missing-body path, where there is no body to spy on.
    """
    closes: list[int] = []

    class _Tracking(Response):
        __slots__ = ()

        def close(self) -> None:
            closes.append(1)
            super().close()

    resp = _Tracking(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)
    return resp, closes


class _IoErrorBody(ResponseBody):
    """A body whose read fails with a genuine ``OSError`` (real I/O failure)."""

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        raise OSError("the socket went away mid-read")

    def close(self) -> None:
        self.closed = True


def _json_body(data: bytes) -> ResponseBody:
    return ResponseBody.from_bytes(data, MediaType.parse("application/json"))


# --------------------------------------------------------------------------- #
# The pure classification decision — status band only, no side effects.        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [200, 201, 204, 206, 226, 299], ids=str)
def test_classify_status_maps_2xx_to_success(status: int) -> None:
    assert classify_status(Status(status)) is ResponseDisposition.SUCCESS


@pytest.mark.parametrize("status", [400, 404, 409, 418, 451, 500, 503, 599], ids=str)
def test_classify_status_maps_4xx_5xx_to_error(status: int) -> None:
    assert classify_status(Status(status)) is ResponseDisposition.ERROR


@pytest.mark.parametrize("status", [100, 101, 103, 300, 301, 302, 304, 308, 399], ids=str)
def test_classify_status_maps_1xx_3xx_to_unexpected(status: int) -> None:
    assert classify_status(Status(status)) is ResponseDisposition.UNEXPECTED


def test_classify_status_is_side_effect_free() -> None:
    # The pure decision never touches the body: classifying a response backed
    # by a body that would explode on read must not read (or close) it.
    resp = _response(Status.OK, body=_IoErrorBody())
    assert classify_status(resp.status) is ResponseDisposition.SUCCESS
    assert isinstance(resp.body, _IoErrorBody)
    assert resp.body.closed is False


# --------------------------------------------------------------------------- #
# DecodeBody orchestration — decode regardless of status; close on EVERY path. #
# --------------------------------------------------------------------------- #


def test_decode_body_decodes_into_target() -> None:
    resp = _response(Status.OK, body=_json_body(b'{"name":"Rex","age":3}'))
    assert DecodeBody()(resp, _Pet) == _Pet(name="Rex", age=3)


def test_decode_body_decodes_generic_target() -> None:
    resp = _response(Status.OK, body=_json_body(b'[{"name":"Rex","age":3}]'))
    assert DecodeBody()(resp, list[_Pet]) == [_Pet(name="Rex", age=3)]


@pytest.mark.parametrize(
    ("make_body", "expected"),
    [
        (lambda: _json_body(b'{"name":"Rex","age":3}'), None),  # success
        (lambda: _json_body(b'{"name":"Rex"}'), DeserializationError),  # codec failure
        (lambda: None, DeserializationError),  # missing body
    ],
    ids=["success", "codec-failure", "missing-body"],
)
def test_decode_body_closes_response_on_every_path(
    make_body: Any, expected: type[Exception] | None
) -> None:
    resp, closes = _tracking_response(Status.OK, body=make_body())
    if expected is None:
        assert DecodeBody()(resp, _Pet) == _Pet(name="Rex", age=3)
    else:
        with pytest.raises(expected):
            DecodeBody()(resp, _Pet)
    assert closes == [1]  # closed exactly once, on every path


def test_decode_body_missing_body_names_the_target_type() -> None:
    resp = _response(Status.OK, body=None)
    with pytest.raises(DeserializationError, match="_Pet") as excinfo:
        DecodeBody()(resp, _Pet)
    # A serde-family error, not a response-branch HttpResponseError.
    assert isinstance(excinfo.value, SerdeError)
    assert not isinstance(excinfo.value, HttpResponseError)


def test_decode_body_codec_failure_is_chained_not_swallowed() -> None:
    resp = _response(Status.OK, body=_json_body(b"not valid json"))
    with pytest.raises(SerdeError) as excinfo:
        DecodeBody()(resp, _Pet)
    # Chained: the underlying serde/codec failure is preserved as the cause.
    assert excinfo.value.__cause__ is not None


@pytest.mark.req("SERDE-12")
def test_decode_body_propagates_genuine_oserror_unwrapped() -> None:
    resp, closes = _tracking_response(Status.OK, body=_IoErrorBody())
    with pytest.raises(OSError) as excinfo:
        DecodeBody()(resp, _Pet)
    # A real I/O failure is NOT a decode-shape problem: it stays an OSError and
    # is never re-wrapped onto the serde branch.
    assert not isinstance(excinfo.value, SerdeError)
    assert closes == [1]  # still closed


def test_decode_body_is_positional_only() -> None:
    resp = _response(Status.OK, body=_json_body(b'{"name":"Rex","age":3}'))
    with pytest.raises(TypeError):
        DecodeBody()(response=resp, response_type=_Pet)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# DecodeSuccess orchestration — 2xx decodes; 4xx/5xx mapped; 1xx/3xx status-led.#
# --------------------------------------------------------------------------- #


class _ServiceError(HttpResponseError[Any]):
    """Synthetic service base error on the response branch only."""


class _NotFoundError(_ServiceError):
    """The example service's 404 error."""


@pytest.mark.req("SERDE-28")
def test_decode_success_decodes_2xx_body() -> None:
    resp, closes = _tracking_response(Status.OK, body=_json_body(b'{"name":"Rex","age":3}'))
    handler = DecodeSuccess(StatusErrorMap())
    assert handler(resp, _Pet) == _Pet(name="Rex", age=3)
    assert closes == [1]  # the success path still closes the response


def test_decode_success_2xx_without_body_names_target() -> None:
    resp, closes = _tracking_response(Status.NO_CONTENT, body=None)
    handler = DecodeSuccess(StatusErrorMap())
    with pytest.raises(DeserializationError, match="_Pet"):
        handler(resp, _Pet)
    assert closes == [1]


@pytest.mark.req("SERDE-28")
def test_decode_success_raises_mapped_error_for_client_error() -> None:
    errors = StatusErrorMap(by_status={Status.NOT_FOUND: _NotFoundError})
    resp, closes = _tracking_response(Status.NOT_FOUND, body=_json_body(b'{"error":"missing"}'))
    handler = DecodeSuccess(errors)
    with pytest.raises(_NotFoundError):
        handler(resp, _Pet)
    assert closes == [1]


@pytest.mark.req("SERDE-28")
def test_decode_success_error_carries_bounded_replayable_body() -> None:
    # The 4xx/5xx path buffers a bounded, replayable copy of the body via
    # StatusErrorMap.raise_for.
    payload = b"x" * (ERROR_BODY_MAX_BYTES + 4096)
    resp = _response(Status.INTERNAL_SERVER_ERROR, body=ResponseBody.from_bytes(payload))
    handler = DecodeSuccess(StatusErrorMap())
    with pytest.raises(HttpResponseError) as excinfo:
        handler(resp, _Pet)
    err = excinfo.value
    first = err.read_body()
    assert len(first) == ERROR_BODY_MAX_BYTES  # bounded to the documented cap
    assert err.read_body() == first  # replayable


@pytest.mark.req("SERDE-28")
@pytest.mark.parametrize(
    "status",
    [Status.CONTINUE, Status.SWITCHING_PROTOCOLS, Status.MOVED_PERMANENTLY, Status.NOT_MODIFIED],
    ids=["100", "101", "301", "304"],
)
def test_decode_success_unexpected_status_leads_message_with_code(status: Status) -> None:
    resp, closes = _tracking_response(status, body=_json_body(b'{"name":"Rex","age":3}'))
    handler = DecodeSuccess(StatusErrorMap())
    with pytest.raises(HttpResponseError) as excinfo:
        handler(resp, _Pet)
    # The status code LEADS the error message.
    assert str(excinfo.value).startswith(str(int(status)))
    assert closes == [1]  # closed on the unexpected path too


def test_decode_success_unexpected_status_does_not_consult_the_error_map() -> None:
    # raise_for rejects a non-error status; the unexpected branch must not route
    # a 3xx through it (that would surface a ValueError instead of a clean raise).
    resp = _response(Status.FOUND, body=_json_body(b"{}"))
    handler = DecodeSuccess(StatusErrorMap(default=_ServiceError))
    with pytest.raises(HttpResponseError) as excinfo:
        handler(resp, _Pet)
    assert not isinstance(excinfo.value, ValueError)
    assert str(excinfo.value).startswith("302")


def test_decode_success_dispatch_matches_the_pure_classifier() -> None:
    # The handler dispatches on exactly the pure decision — a 2xx is SUCCESS and
    # decodes; a 4xx is ERROR and raises; the two never disagree.
    assert classify_status(Status.OK) is ResponseDisposition.SUCCESS
    assert classify_status(Status.BAD_REQUEST) is ResponseDisposition.ERROR
    ok = _response(Status.OK, body=_json_body(b'{"name":"Rex","age":3}'))
    assert DecodeSuccess(StatusErrorMap())(ok, _Pet) == _Pet("Rex", 3)
    bad = _response(Status.BAD_REQUEST, body=_json_body(b"{}"))
    with pytest.raises(HttpResponseError):
        DecodeSuccess(StatusErrorMap())(bad, _Pet)


# --------------------------------------------------------------------------- #
# Protocol conformance — handlers are swappable ResponseHandlers.              #
# --------------------------------------------------------------------------- #


def test_shipped_handlers_satisfy_the_protocol() -> None:
    assert isinstance(DecodeBody(), ResponseHandler)
    assert isinstance(DecodeSuccess(StatusErrorMap()), ResponseHandler)


def test_alternate_handler_can_be_swapped_in() -> None:
    # A raw-download handler ignores the target and returns bytes — a valid
    # ResponseHandler that does no decoding.
    class _RawBytes:
        def __call__(self, response: Response, response_type: Any, /) -> Any:
            try:
                return response.body.bytes() if response.body is not None else b""
            finally:
                response.close()

    handler: ResponseHandler = _RawBytes()
    resp = _response(Status.OK, body=_json_body(b"raw-payload"))
    assert handler(resp, _Pet) == b"raw-payload"
