"""Round-trip tests for the small HTTP enums."""
from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Protocol
from dexpace.sdk.core.http.request import Method
from dexpace.sdk.core.http.response import Status


def test_method_str_round_trip() -> None:
    assert Method("GET") is Method.GET
    assert str(Method.POST) == "POST"


def test_method_is_str_compatible() -> None:
    assert Method.GET == "GET"


def test_status_is_intenum() -> None:
    assert int(Status.OK) == 200
    assert Status(404) is Status.NOT_FOUND


def test_status_is_success_property() -> None:
    assert Status.OK.is_success
    assert not Status.NOT_FOUND.is_success
    assert not Status.INTERNAL_SERVER_ERROR.is_success


@pytest.mark.parametrize("status,success", [
    (Status.OK, True),
    (Status.CREATED, True),
    (Status.NO_CONTENT, True),
    (Status.MULTIPLE_CHOICES, False),
    (Status.BAD_REQUEST, False),
    (Status.NOT_FOUND, False),
    (Status.INTERNAL_SERVER_ERROR, False),
])
def test_status_success_band(status: Status, success: bool) -> None:
    assert status.is_success is success


def test_protocol_round_trip() -> None:
    assert Protocol.parse("http/1.1") is Protocol.HTTP_1_1
    assert Protocol.parse("HTTP/1.1") is Protocol.HTTP_1_1
    assert str(Protocol.HTTP_1_1) == "http/1.1"


def test_protocol_h2_aliases() -> None:
    assert Protocol.parse("h2") is Protocol.HTTP_2
    assert Protocol.parse("http/2.0") is Protocol.HTTP_2


def test_protocol_unknown_raises() -> None:
    with pytest.raises(ValueError):
        Protocol.parse("ftp/1.0")
