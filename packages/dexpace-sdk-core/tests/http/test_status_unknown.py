# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for lenient `Status` lookup of unregistered HTTP codes."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.response import Status


def test_unknown_in_range_code_is_synthesized() -> None:
    status = Status(218)
    assert isinstance(status, Status)
    assert int(status) == 218
    assert status == 218
    assert status.name == "UNKNOWN_218"
    assert status.value == 218


def test_unknown_success_code_classifies_as_success() -> None:
    status = Status(218)
    assert status.is_success
    assert not status.is_redirect
    assert not status.is_error


def test_unknown_server_error_code_classifies_as_server_error() -> None:
    status = Status(599)
    assert int(status) == 599
    assert status.name == "UNKNOWN_599"
    assert status.is_server_error
    assert status.is_error
    assert not status.is_success


@pytest.mark.parametrize(
    "code,predicate",
    [
        (199, "is_informational"),
        (250, "is_success"),
        (399, "is_redirect"),
        (450, "is_client_error"),
        (550, "is_server_error"),
    ],
)
def test_unknown_codes_band_classification(code: int, predicate: str) -> None:
    status = Status(code)
    assert getattr(status, predicate)


def test_known_code_still_returns_named_member() -> None:
    assert Status(200) is Status.OK
    assert Status(404) is Status.NOT_FOUND


@pytest.mark.parametrize("code", [42, 99, 600, 1000, -1])
def test_out_of_range_code_still_raises(code: int) -> None:
    with pytest.raises(ValueError):
        Status(code)
