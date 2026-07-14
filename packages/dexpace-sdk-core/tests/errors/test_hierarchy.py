# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the SDK exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_type

import pytest

from dexpace.sdk.core.errors import (
    ClientAuthenticationError,
    DecodeError,
    DeserializationError,
    HttpResponseError,
    NetworkError,
    PipelineAbortedError,
    RequestCancelledError,
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


def test_sdk_error_traceback_falls_back_to_cause() -> None:
    # Capture a caught exception, then construct the SdkError *outside* its
    # except block so ``sys.exc_info()`` is the empty (None, None, None)
    # triple. The traceback must still fall back to the cause's own
    # ``__traceback__`` rather than leaving an incoherent (type, value, None).
    cause: RuntimeError
    try:
        raise RuntimeError("inner")
    except RuntimeError as exc:
        cause = exc
    assert cause.__traceback__ is not None
    err = SdkError("outer", error=cause)
    assert err.exc_type is RuntimeError
    assert err.exc_value is cause
    assert err.exc_traceback is cause.__traceback__


def test_continuation_token_propagates() -> None:
    err = SdkError("paging", continuation_token="next-page-3")
    assert err.continuation_token == "next-page-3"


def test_sdk_error_rejects_extra_positional_args() -> None:
    with pytest.raises(TypeError):
        SdkError("msg", "extra")  # type: ignore[misc,arg-type]


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


# ----- transport branch: NetworkError(OSError) ----------------------------


def test_network_error_is_both_sdk_error_and_os_error() -> None:
    # NetworkError joins the SDK root and OSError so ``except OSError:`` sites
    # (including user code) keep matching transport-level failures.
    assert issubclass(NetworkError, SdkError)
    assert issubclass(NetworkError, OSError)


def test_service_request_error_is_a_network_error() -> None:
    # "Request never reached the service" is the transport / never-got-a-response
    # branch, so it lives under NetworkError and is therefore an OSError.
    assert issubclass(ServiceRequestError, NetworkError)
    assert issubclass(ServiceRequestError, OSError)
    assert issubclass(ServiceRequestTimeoutError, NetworkError)


def test_service_response_error_stays_off_the_network_branch() -> None:
    # A response *was* received (just unparseable); it is not the
    # never-got-a-response branch, so it must not be a NetworkError and must
    # not be always-retryable.
    assert not issubclass(ServiceResponseError, NetworkError)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NetworkError("boom"),
        lambda: ServiceRequestError("connect refused"),
        lambda: ServiceRequestTimeoutError("connect timed out"),
        lambda: RequestCancelledError("cancelled"),
    ],
    ids=[
        "NetworkError",
        "ServiceRequestError",
        "ServiceRequestTimeoutError",
        "RequestCancelledError",
    ],
)
def test_except_oserror_catches_transport_and_cancel(factory: object) -> None:
    with pytest.raises(OSError):
        raise factory()  # type: ignore[operator]


def test_network_error_is_always_retryable_at_class_level() -> None:
    # Transport failures that never got a response are always retryable at the
    # error-class level — distinct from the per-status classifier.
    assert NetworkError("boom").retryable is True
    assert ServiceRequestError("connect refused").retryable is True
    assert ServiceRequestTimeoutError("connect timed out").retryable is True


def test_request_cancelled_is_oserror_but_not_network_or_retryable() -> None:
    cancel = RequestCancelledError("cancelled")
    assert isinstance(cancel, OSError)
    assert isinstance(cancel, SdkError)
    # Cancellation is terminal: not a NetworkError and never retryable.
    assert not isinstance(cancel, NetworkError)
    assert cancel.retryable is False


def test_timeout_and_cancellation_discriminated_by_class_not_message() -> None:
    # Identical message text on purpose: discrimination must be by type only.
    timeout = ServiceRequestTimeoutError("deadline exceeded")
    cancel = RequestCancelledError("deadline exceeded")
    assert str(timeout) == str(cancel)
    # Neither is an ancestor of the other, so a subtype-first check (timeout
    # before cancellation) never has one swallow the other.
    assert not issubclass(ServiceRequestTimeoutError, RequestCancelledError)
    assert not issubclass(RequestCancelledError, ServiceRequestTimeoutError)
    assert isinstance(timeout, ServiceRequestTimeoutError)
    assert not isinstance(timeout, RequestCancelledError)
    assert isinstance(cancel, RequestCancelledError)
    assert not isinstance(cancel, ServiceRequestTimeoutError)
    # The retryable timeout is checked (and wins) before the terminal cancel.
    assert timeout.retryable is True
    assert cancel.retryable is False


def test_sdk_timeout_type_is_not_the_builtin_timeouterror() -> None:
    # The SDK's mapping is explicit: a NetworkError-branch timeout is an OSError
    # by design, not by coincidental reuse of Python's builtin ``TimeoutError``
    # (which is itself an OSError since 3.10).
    assert not issubclass(ServiceRequestTimeoutError, TimeoutError)
    assert not issubclass(RequestCancelledError, TimeoutError)


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


@dataclass(frozen=True, slots=True)
class _ErrorPayload:
    """Toy error-body schema used to exercise the generic ``model`` field."""

    code: str
    message: str


def test_http_response_error_model_defaults_to_none() -> None:
    err = HttpResponseError(response=_response(Status.BAD_REQUEST))
    assert err.model is None


def test_http_response_error_carries_typed_model_payload() -> None:
    payload = _ErrorPayload(code="E001", message="bad input")
    err: HttpResponseError[_ErrorPayload] = HttpResponseError(
        response=_response(Status.BAD_REQUEST),
        model=payload,
    )
    assert err.model is payload
    # The real value here is mypy inference: ``err.model`` is typed as
    # ``_ErrorPayload | None`` so attribute access is narrowed below.
    assert err.model is not None
    assert err.model.code == "E001"
    assert_type(err.model, _ErrorPayload)


def test_unparametrised_http_response_error_model_is_any() -> None:
    # Unparametrised construction defaults to ``HttpResponseError[Any]``
    # under PEP 696 so the historical ``Any``-typed ``model`` field still
    # works without an explicit type argument.
    err = HttpResponseError(response=_response(Status.BAD_REQUEST), model=object())
    assert err.model is not None


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
