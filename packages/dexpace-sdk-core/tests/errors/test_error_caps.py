# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``retryable`` flag and non-consuming ``body_snapshot``.

Covers the two capabilities added to ``HttpResponseError``: a status-derived
``retryable`` flag the retry policy can read directly, and a
``body_snapshot`` preview that never drains a single-use response body.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.errors import HttpResponseError, ResourceNotFoundError
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import (
    LoggableResponseBody,
    Response,
    ResponseBody,
    Status,
)


def _response(status: Status, *, body: ResponseBody | None = None) -> Response:
    request = Request(method=Method.GET, url=Url.parse("https://example.com/"))
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        body=body,
    )


# ----- retryable flag -----------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        Status.REQUEST_TIMEOUT,
        Status.TOO_MANY_REQUESTS,
        Status.INTERNAL_SERVER_ERROR,
        Status.BAD_GATEWAY,
        Status.SERVICE_UNAVAILABLE,
        Status.GATEWAY_TIMEOUT,
    ],
    ids=["408", "429", "500", "502", "503", "504"],
)
def test_retryable_is_true_for_transient_status(status: Status) -> None:
    err = HttpResponseError(response=_response(status))
    assert err.retryable is True


@pytest.mark.parametrize(
    "status",
    [Status.BAD_REQUEST, Status.NOT_FOUND, Status.CONFLICT, Status.NOT_IMPLEMENTED],
    ids=["400", "404", "409", "501"],
)
def test_retryable_is_false_for_terminal_status(status: Status) -> None:
    err = HttpResponseError(response=_response(status))
    assert err.retryable is False


def test_retryable_is_false_when_no_response() -> None:
    err = HttpResponseError("no response captured")
    assert err.retryable is False


def test_retryable_override_forces_true() -> None:
    err = HttpResponseError(response=_response(Status.NOT_FOUND), retryable=True)
    assert err.retryable is True


def test_retryable_override_forces_false() -> None:
    err = HttpResponseError(response=_response(Status.SERVICE_UNAVAILABLE), retryable=False)
    assert err.retryable is False


def test_retryable_inherited_by_subclasses() -> None:
    err = ResourceNotFoundError(response=_response(Status.NOT_FOUND))
    assert err.retryable is False


# ----- body_snapshot ------------------------------------------------------


def test_body_snapshot_previews_loggable_body_without_consuming() -> None:
    loggable = LoggableResponseBody(
        ResponseBody.from_bytes(b'{"error":"boom"}', MediaType.parse("application/json")),
    )
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=loggable))

    preview = err.body_snapshot()

    assert preview == b'{"error":"boom"}'
    # Preview must not consume: the body is still readable afterwards.
    assert loggable.bytes() == b'{"error":"boom"}'


def test_body_snapshot_truncates_to_max_bytes() -> None:
    loggable = LoggableResponseBody(ResponseBody.from_bytes(b"0123456789"))
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=loggable))

    assert err.body_snapshot(4) == b"0123"


def test_body_snapshot_returns_empty_for_single_use_body() -> None:
    # A plain bytes-backed body is single-use; previewing it must not drain it.
    body = ResponseBody.from_bytes(b"unsafe-to-peek")
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=body))

    assert err.body_snapshot() == b""
    # The underlying body is untouched and still fully readable.
    assert body.bytes() == b"unsafe-to-peek"


def test_body_snapshot_returns_empty_when_no_body() -> None:
    err = HttpResponseError(response=_response(Status.BAD_REQUEST))
    assert err.body_snapshot() == b""


def test_body_snapshot_returns_empty_when_no_response() -> None:
    err = HttpResponseError("no response captured")
    assert err.body_snapshot() == b""


def test_body_snapshot_rejects_negative_max_bytes() -> None:
    loggable = LoggableResponseBody(ResponseBody.from_bytes(b"data"))
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=loggable))

    with pytest.raises(ValueError, match="max_bytes must be non-negative"):
        err.body_snapshot(-1)
