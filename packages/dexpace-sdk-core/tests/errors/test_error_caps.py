# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``retryable`` flag and non-consuming ``body_snapshot``.

Covers the two capabilities added to ``HttpResponseError``: a status-derived
``retryable`` flag the retry policy can read directly, and a
``body_snapshot`` preview that never drains a single-use response body.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.errors import (
    ERROR_BODY_MAX_BYTES,
    HttpResponseError,
    ResourceNotFoundError,
)
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import (
    LoggableResponseBody,
    Response,
    ResponseBody,
    Status,
)
from dexpace.sdk.core.pipeline._pure import classifier


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


def test_retryable_is_baked_once_and_stable_against_classifier_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The flag is derived from the shared classifier at construction time and
    # then baked as a plain attribute, so mutating the classifier's configured
    # set afterwards must not retroactively change an already-built error.
    err = HttpResponseError(response=_response(Status.SERVICE_UNAVAILABLE))
    assert err.retryable is True
    baked = err.retryable

    # Repoint the classifier's default set at an empty set *after* construction.
    monkeypatch.setattr(classifier, "DEFAULT_RETRYABLE_STATUS", frozenset())

    # The already-constructed error keeps its baked value (no recompute).
    assert err.retryable is baked is True
    # Proof the patch is live: a freshly built error now reflects the empty set,
    # confirming the first error's flag was frozen at its own construction.
    fresh = HttpResponseError(response=_response(Status.SERVICE_UNAVAILABLE))
    assert fresh.retryable is False


# ----- bounded, replayable error body -------------------------------------


def test_error_body_cap_is_one_mib() -> None:
    assert ERROR_BODY_MAX_BYTES == 1 << 20  # documented 1 MiB cap


def test_read_body_captures_bounded_replayable_copy() -> None:
    # A body larger than the cap is truncated to the documented ceiling and the
    # captured copy replays identically on every subsequent read.
    payload = b"x" * (ERROR_BODY_MAX_BYTES + 4096)
    body = ResponseBody.from_bytes(payload)
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=body))

    first = err.read_body()
    assert len(first) == ERROR_BODY_MAX_BYTES
    # Replayable: the error carries the bounded copy, so a second read matches.
    assert err.read_body() == first


def test_read_body_returns_full_body_under_cap() -> None:
    body = ResponseBody.from_bytes(b'{"error":"boom"}')
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=body))
    assert err.read_body() == b'{"error":"boom"}'
    assert err.read_body() == b'{"error":"boom"}'  # replayable


def test_read_body_reads_from_loggable_body_repeatably() -> None:
    loggable = LoggableResponseBody(
        ResponseBody.from_bytes(b'{"error":"boom"}', MediaType.parse("application/json")),
    )
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=loggable))
    assert err.read_body() == b'{"error":"boom"}'
    # The caller-owned loggable body is still readable afterwards.
    assert loggable.bytes() == b'{"error":"boom"}'


def test_read_body_honours_explicit_smaller_cap() -> None:
    body = ResponseBody.from_bytes(b"0123456789")
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=body))
    assert err.read_body(4) == b"0123"


def test_read_body_returns_empty_when_no_body() -> None:
    err = HttpResponseError(response=_response(Status.BAD_REQUEST))
    assert err.read_body() == b""


def test_read_body_returns_empty_when_no_response() -> None:
    err = HttpResponseError("no response captured")
    assert err.read_body() == b""


def test_read_body_rejects_negative_max_bytes() -> None:
    body = ResponseBody.from_bytes(b"data")
    err = HttpResponseError(response=_response(Status.BAD_REQUEST, body=body))
    with pytest.raises(ValueError, match="max_bytes must be non-negative"):
        err.read_body(-1)


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
