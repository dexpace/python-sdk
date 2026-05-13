"""Tests for the SDK exception hierarchy."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.errors import (
    ClientAuthenticationError,
    DecodeError,
    DeserializationError,
    HttpResponseError,
    PipelineAbortedError,
    ResourceExistsError,
    ResourceNotFoundError,
    ResponseNotReadError,
    SdkError,
    SerializationError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
    StreamClosedError,
    StreamConsumedError,
    map_error,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status


def _response(status: Status) -> Response:
    request = Request(method=Method.GET, url=Url.parse("https://example.com/"))
    return Response(request=request, protocol=Protocol.HTTP_1_1, status=status)


def test_sdk_error_captures_inner_exception() -> None:
    cause = ValueError("boom")
    err = SdkError("wrap", error=cause)
    assert err.inner_exception is cause


def test_sdk_error_captures_sys_exc_info() -> None:
    try:
        raise RuntimeError("inner")
    except RuntimeError:
        err = SdkError("outer")
    assert err.exc_type is RuntimeError
    assert err.exc_value is not None
    assert "inner" in str(err.exc_value)


def test_continuation_token_propagates() -> None:
    err = SdkError("paging", continuation_token="next-page-3")
    assert err.continuation_token == "next-page-3"


def test_http_response_error_carries_status_and_response() -> None:
    response = _response(Status.NOT_FOUND)
    err = HttpResponseError(response=response)
    assert err.status is Status.NOT_FOUND
    assert err.response is response
    assert "NOT_FOUND" in err.message


def test_http_response_error_with_explicit_message() -> None:
    err = HttpResponseError("custom msg", response=_response(Status.OK))
    assert err.message == "custom msg"


def test_request_response_errors_are_distinct() -> None:
    assert not issubclass(ServiceRequestError, ServiceResponseError)
    assert not issubclass(ServiceResponseError, ServiceRequestError)
    assert issubclass(ServiceRequestTimeoutError, ServiceRequestError)
    assert issubclass(ServiceResponseTimeoutError, ServiceResponseError)


def test_resource_errors_extend_http_response_error() -> None:
    assert issubclass(ResourceExistsError, HttpResponseError)
    assert issubclass(ResourceNotFoundError, HttpResponseError)
    assert issubclass(ClientAuthenticationError, HttpResponseError)


def test_serialization_errors_are_also_value_errors() -> None:
    assert issubclass(SerializationError, ValueError)
    assert issubclass(DeserializationError, ValueError)
    # And SdkError.
    assert issubclass(SerializationError, SdkError)
    assert issubclass(DeserializationError, SdkError)


def test_streaming_errors_have_helpful_default_messages() -> None:
    consumed = StreamConsumedError()
    closed = StreamClosedError()
    notread = ResponseNotReadError()
    assert "already been consumed" in consumed.message
    assert "is closed" in closed.message
    assert "has not been read" in notread.message


def test_pipeline_aborted_error_is_sdk_error() -> None:
    err = PipelineAbortedError("step returned None")
    assert isinstance(err, SdkError)


def test_decode_error_extends_http_response_error() -> None:
    assert issubclass(DecodeError, HttpResponseError)


class TestMapError:
    def test_raises_mapped_error(self) -> None:
        response = _response(Status.NOT_FOUND)
        with pytest.raises(ResourceNotFoundError):
            map_error(404, response, {404: ResourceNotFoundError})

    def test_noop_when_status_absent(self) -> None:
        response = _response(Status.OK)
        map_error(200, response, {404: ResourceNotFoundError})  # no raise

    def test_noop_when_map_is_none(self) -> None:
        response = _response(Status.NOT_FOUND)
        map_error(404, response, None)  # no raise
