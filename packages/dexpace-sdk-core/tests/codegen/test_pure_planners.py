# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Unit tests for the shared pure planners under ``codegen/_pure``.

These are the awaiting-free functions both executor shells import, so testing
them once certifies the logic for both the sync and async twins.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.codegen import Operation, OperationInput
from dexpace.sdk.core.codegen._pure.options import AUTH_OPTION_KEY, plan_options
from dexpace.sdk.core.codegen._pure.requests import plan_request
from dexpace.sdk.core.codegen._pure.results import plan_result
from dexpace.sdk.core.http.request import Method
from dexpace.sdk.core.http.response import Response

from .support import Pet

# --------------------------------------------------------------------------- #
# plan_request                                                                 #
# --------------------------------------------------------------------------- #


def test_plan_request_binds_operation_and_input() -> None:
    op = Operation(name="getPet", method=Method.GET, path="/pets/{id}")
    req = plan_request(
        op, OperationInput(path_params={"id": "7"}), "https://api.example.com/v1", None
    )
    assert req.method is Method.GET
    assert str(req.url) == "https://api.example.com/v1/pets/7"


def test_plan_request_reports_missing_path_param() -> None:
    op = Operation(name="getPet", method=Method.GET, path="/pets/{id}")
    with pytest.raises(KeyError):
        plan_request(op, OperationInput(), "https://api.example.com", None)


# --------------------------------------------------------------------------- #
# plan_options                                                                 #
# --------------------------------------------------------------------------- #


def test_plan_options_merges_call_options() -> None:
    opts = plan_options(None, {"marker": "abc", "retry_total": 5})
    assert opts["marker"] == "abc"
    assert opts["retry_total"] == 5
    assert AUTH_OPTION_KEY not in opts


def test_plan_options_parks_auth_under_reserved_key() -> None:
    sentinel = object()
    opts = plan_options(sentinel, {})
    assert opts[AUTH_OPTION_KEY] is sentinel


def test_plan_options_auth_wins_over_same_named_call_option() -> None:
    sentinel = object()
    opts = plan_options(sentinel, {AUTH_OPTION_KEY: "shadowed"})
    assert opts[AUTH_OPTION_KEY] is sentinel


def test_plan_options_returns_a_read_only_mapping() -> None:
    opts = plan_options(None, {"a": 1})
    with pytest.raises(TypeError):
        opts["b"] = 2  # type: ignore[index]


# --------------------------------------------------------------------------- #
# plan_result                                                                  #
# --------------------------------------------------------------------------- #


def test_plan_result_returns_none_for_a_raw_call() -> None:
    assert plan_result(None, "default", None, Response) is None


def test_plan_result_uses_default_handler_and_response_type() -> None:
    assert plan_result(None, "default", Pet, Response) == ("default", Pet)


def test_plan_result_custom_handler_wins_over_default() -> None:
    assert plan_result("custom", "default", Pet, Response) == ("custom", Pet)


def test_plan_result_custom_handler_with_no_type_uses_default_witness() -> None:
    assert plan_result("custom", "default", None, Response) == ("custom", Response)
