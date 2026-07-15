# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``StatusErrorMap`` — data-driven status → typed-error mapping.

Covers the four behaviours the codegen error contract guarantees:

- ``raise_for`` rejects a non-error (2xx / 3xx / 1xx) response instead of
  fabricating a success-looking exception.
- a per-status override wins over the fallback ``default`` class.
- a constructed error carries a bounded, replayable copy of the response body.
- the map stays on the ``HttpResponseError`` branch only, so a downstream
  service taxonomy can never catch a ``NetworkError`` and re-wrap it off the
  ``OSError`` branch.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError

import pytest

from dexpace.sdk.core.codegen import StatusErrorMap
from dexpace.sdk.core.errors import (
    ERROR_BODY_MAX_BYTES,
    HttpResponseError,
    NetworkError,
    ResourceNotFoundError,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status


def _response(status: Status, *, body: ResponseBody | None = None) -> Response:
    request = Request(method=Method.GET, url=Url.parse("https://example.com/"))
    return Response(request=request, protocol=Protocol.HTTP_1_1, status=status, body=body)


# --------------------------------------------------------------------------- #
# A synthetic downstream service taxonomy — response branch ONLY.              #
# --------------------------------------------------------------------------- #


class _ExampleServiceError(HttpResponseError):
    """Synthetic service base error; extends the response branch only."""


class _ExampleNotFoundError(_ExampleServiceError):
    """The example service's 404 error."""


class _ExampleConflictError(_ExampleServiceError):
    """The example service's 409 error."""


# --------------------------------------------------------------------------- #
# raise_for rejects a non-error response.                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.req("BODY-31", "XCUT-8")
@pytest.mark.parametrize(
    "status",
    [Status.OK, Status.CREATED, Status.NO_CONTENT, Status.NOT_MODIFIED, Status.FOUND],
    ids=["200", "201", "204", "304", "302"],
)
def test_raise_for_rejects_non_error_status(status: Status) -> None:
    # A 2xx/3xx/1xx status is a caller error, never a fabricated exception.
    m = StatusErrorMap()
    with pytest.raises(ValueError, match="error"):
        m.raise_for(_response(status))


# --------------------------------------------------------------------------- #
# Precedence: a per-status override wins over the fallback default.            #
# --------------------------------------------------------------------------- #


def test_specific_status_wins_over_default_when_raising() -> None:
    m = StatusErrorMap(
        by_status={Status.NOT_FOUND: _ExampleNotFoundError},
        default=_ExampleServiceError,
    )
    # 404 is mapped explicitly -> the override class.
    with pytest.raises(_ExampleNotFoundError):
        m.raise_for(_response(Status.NOT_FOUND))
    # 400 is not mapped -> the fallback default (but not the override).
    with pytest.raises(_ExampleServiceError) as excinfo:
        m.raise_for(_response(Status.BAD_REQUEST))
    assert not isinstance(excinfo.value, _ExampleNotFoundError)


def test_specific_status_wins_over_default_in_for_status() -> None:
    m = StatusErrorMap(
        by_status={Status.NOT_FOUND: ResourceNotFoundError},
        default=_ExampleServiceError,
    )
    assert m.for_status(Status.NOT_FOUND) is ResourceNotFoundError
    assert m.for_status(Status.BAD_REQUEST) is _ExampleServiceError


# --------------------------------------------------------------------------- #
# for_status is a permissive, side-effect-free peek.                           #
# --------------------------------------------------------------------------- #


@pytest.mark.req("BODY-31", "XCUT-8")
@pytest.mark.parametrize(
    "status",
    [200, 201, 204, 100, 301, 302, 304, 399],
    ids=["200", "201", "204", "100", "301", "302", "304", "399"],
)
def test_for_status_returns_none_for_non_error(status: int) -> None:
    assert StatusErrorMap(default=_ExampleServiceError).for_status(status) is None


@pytest.mark.parametrize("status", [400, 404, 409, 418, 500, 503, 599])
def test_for_status_returns_default_for_unmapped_error(status: int) -> None:
    m = StatusErrorMap(default=_ExampleServiceError)
    assert m.for_status(status) is _ExampleServiceError


def test_for_status_default_default_is_http_response_error() -> None:
    # With no explicit default, an unmapped error status maps to the base class.
    assert StatusErrorMap().for_status(500) is HttpResponseError


def test_for_status_accepts_plain_int_and_status_enum() -> None:
    m = StatusErrorMap(by_status={404: _ExampleNotFoundError})
    assert m.for_status(404) is _ExampleNotFoundError
    assert m.for_status(Status.NOT_FOUND) is _ExampleNotFoundError


# --------------------------------------------------------------------------- #
# The map is frozen data: defensive copy + read-only view + frozen instance.   #
# --------------------------------------------------------------------------- #


@pytest.mark.req("XCUT-15")
def test_map_defensively_copies_source_mapping() -> None:
    src: dict[int, type[_ExampleServiceError]] = {404: _ExampleNotFoundError}
    m = StatusErrorMap(by_status=src, default=_ExampleServiceError)
    src[409] = _ExampleConflictError  # mutate the caller's dict after construction
    # The map is unaffected: 409 still falls back to the default.
    assert m.for_status(409) is _ExampleServiceError


def test_map_by_status_is_read_only() -> None:
    m = StatusErrorMap(by_status={404: _ExampleNotFoundError})
    with pytest.raises(TypeError):
        m.by_status[500] = _ExampleConflictError  # type: ignore[index]


def test_map_instance_is_frozen() -> None:
    m = StatusErrorMap()
    with pytest.raises(FrozenInstanceError):
        m.default = _ExampleServiceError  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# A constructed error carries the bounded, replayable buffered body.          #
# --------------------------------------------------------------------------- #


@pytest.mark.req("BODY-30")
def test_raise_for_carries_bounded_replayable_body() -> None:
    payload = b"x" * (ERROR_BODY_MAX_BYTES + 4096)
    m = StatusErrorMap(by_status={Status.BAD_REQUEST: _ExampleNotFoundError})
    with pytest.raises(_ExampleNotFoundError) as excinfo:
        m.raise_for(_response(Status.BAD_REQUEST, body=ResponseBody.from_bytes(payload)))

    err = excinfo.value
    first = err.read_body()
    assert len(first) == ERROR_BODY_MAX_BYTES  # bounded to the documented cap
    assert err.read_body() == first  # replayable: a second read matches


@pytest.mark.req("BODY-30")
def test_raise_for_buffers_body_eagerly_before_raising() -> None:
    # A single-use stream body is drained by raise_for's eager buffering; the
    # caught error still replays the captured copy even though the underlying
    # stream is now exhausted.
    stream = io.BytesIO(b'{"error":"boom"}')
    m = StatusErrorMap()
    with pytest.raises(HttpResponseError) as excinfo:
        m.raise_for(_response(Status.INTERNAL_SERVER_ERROR, body=ResponseBody.from_stream(stream)))

    err = excinfo.value
    assert err.read_body() == b'{"error":"boom"}'
    assert err.read_body() == b'{"error":"boom"}'


def test_raise_for_handles_error_response_without_a_body() -> None:
    m = StatusErrorMap()
    with pytest.raises(HttpResponseError) as excinfo:
        m.raise_for(_response(Status.SERVICE_UNAVAILABLE))
    assert excinfo.value.read_body() == b""


# --------------------------------------------------------------------------- #
# Invariant: the map never re-wraps a NetworkError onto the response branch.   #
# --------------------------------------------------------------------------- #


def test_service_hierarchy_stays_on_the_response_branch() -> None:
    # The example service base extends HttpResponseError and must NOT be an
    # OSError / NetworkError — that is what keeps ``except OSError:`` sites
    # matching transport failures after the SDK raises typed service errors.
    assert issubclass(_ExampleServiceError, HttpResponseError)
    assert not issubclass(_ExampleServiceError, OSError)
    assert not issubclass(_ExampleServiceError, NetworkError)


def test_map_rejects_an_oserror_branch_error_class() -> None:
    # A class straddling both branches would let a service error masquerade as
    # a transport failure; the map refuses to register it at build time.
    class _StraddlingError(HttpResponseError, OSError):
        """Pathological hybrid: must be rejected by StatusErrorMap."""

    with pytest.raises(TypeError, match="OSError"):
        StatusErrorMap(by_status={500: _StraddlingError})
    with pytest.raises(TypeError, match="OSError"):
        StatusErrorMap(default=_StraddlingError)


def test_map_rejects_a_non_response_error_class() -> None:
    with pytest.raises(TypeError, match="HttpResponseError"):
        StatusErrorMap(by_status={500: ValueError})  # type: ignore[dict-item]


def test_network_error_passes_through_a_service_dispatcher_untouched() -> None:
    # A tiny stand-in for a generated dispatcher: received responses are routed
    # through the map, but a transport-level NetworkError is never handed to it.
    service_map = StatusErrorMap(
        by_status={Status.NOT_FOUND: _ExampleNotFoundError},
        default=_ExampleServiceError,
    )

    def dispatch(outcome: Response | BaseException) -> None:
        if isinstance(outcome, BaseException):
            raise outcome  # transport failure — the map is never consulted
        service_map.raise_for(outcome)

    # The NetworkError propagates unchanged: still a NetworkError, still an
    # OSError, and never re-wrapped into the service's response-branch taxonomy.
    with pytest.raises(NetworkError) as excinfo:
        dispatch(NetworkError("connection refused"))
    assert isinstance(excinfo.value, OSError)
    assert not isinstance(excinfo.value, _ExampleServiceError)

    with pytest.raises(OSError):
        dispatch(NetworkError("connection refused"))

    # A received error response still routes to the service taxonomy as normal.
    with pytest.raises(_ExampleNotFoundError):
        dispatch(_response(Status.NOT_FOUND))
