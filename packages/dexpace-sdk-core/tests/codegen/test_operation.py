# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the declarative ``Operation`` / ``OperationInput`` value objects."""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

from dexpace.sdk.core.codegen import Operation, OperationInput
from dexpace.sdk.core.http.common import Headers, QueryParams
from dexpace.sdk.core.http.request import Method, RequestBody


def test_operation_fields_are_keyword_only() -> None:
    # kw_only lets a future minor version append fields without breaking any
    # positional call site — assert every field is genuinely keyword-only.
    assert all(f.kw_only for f in dataclasses.fields(Operation))


def test_operation_input_fields_are_keyword_only() -> None:
    assert all(f.kw_only for f in dataclasses.fields(OperationInput))


def test_operation_is_frozen() -> None:
    op = Operation(name="getUser", method=Method.GET, path="/users/{id}")
    with pytest.raises(FrozenInstanceError):
        op.name = "other"  # type: ignore[misc]


def test_operation_input_is_frozen() -> None:
    inp = OperationInput()
    with pytest.raises(FrozenInstanceError):
        inp.body = None  # type: ignore[misc]


def test_operation_input_defaults_are_empty() -> None:
    inp = OperationInput()
    assert dict(inp.path_params) == {}
    assert inp.query == QueryParams()
    assert inp.headers == Headers()
    assert inp.body is None


def test_name_excluded_from_equality() -> None:
    # ``name`` is purely advisory: two operations that differ only in name are
    # equal, so nothing keyed on an Operation can ever be split apart by name.
    a = Operation(name="getUser", method=Method.GET, path="/users/{id}")
    b = Operation(name="retrieveUser", method=Method.GET, path="/users/{id}")
    assert a == b


def test_name_excluded_from_hash() -> None:
    # Equal hashes guarantee that using an Operation as a dict / store key never
    # partitions two otherwise-identical operations by their advisory name.
    a = Operation(name="getUser", method=Method.GET, path="/users/{id}")
    b = Operation(name="retrieveUser", method=Method.GET, path="/users/{id}")
    assert hash(a) == hash(b)


def test_operation_carries_optional_static_headers() -> None:
    op = Operation(
        name="getUser",
        method=Method.GET,
        path="/users/{id}",
        headers=Headers({"accept": "application/json"}),
    )
    assert op.headers.get("accept") == "application/json"


def test_operation_input_carries_body_through() -> None:
    body = RequestBody.from_string("payload")
    inp = OperationInput(body=body)
    assert inp.body is body
