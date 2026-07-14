# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Declarative operation description for the Tier-1 codegen contract.

`Operation` describes a single request shape a generated client would target —
method, path template, and any static headers — while `OperationInput` supplies
the per-call values (path parameters, query parameters, extra headers, body).
Both are frozen, keyword-only dataclasses so a future minor version can append
fields without breaking any positional call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..http.common import Headers, QueryParams
from ..http.request import Method

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..http.request import RequestBody

__all__ = ["Operation", "OperationInput"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Operation:
    """A declarative description of one request shape (the static half).

    An `Operation` is the reusable, call-independent half of the contract: it
    names the method and the path template and carries any headers that are
    constant for every invocation. The per-call values live in `OperationInput`
    and are combined with the operation by `assemble`.

    Keyword-only construction is deliberate — it lets a future minor version
    append a field without breaking positional call sites, which is the
    forward-compatibility guarantee a generated client depends on.

    Attributes:
        name: An advisory label used only to stamp tracing / diagnostic context
            for readability. It NEVER influences request assembly, dispatch
            routing, or any cache / store key, and is excluded from equality and
            hashing so two operations differing only in ``name`` compare equal.
        method: The HTTP method for the request.
        path: The path template appended to the base URL. May contain
            ``{placeholder}`` tokens that `assemble` substitutes from
            `OperationInput.path_params`.
        headers: Static headers constant across every call. Per-call headers in
            `OperationInput.headers` are overlaid on top of these by `assemble`.
    """

    name: str = field(compare=False)
    method: Method
    path: str
    headers: Headers = field(default_factory=Headers)


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationInput:
    """The per-call values combined with an `Operation` to build a request.

    Every field defaults to empty, so a parameterless operation needs only
    ``OperationInput()``. Keyword-only construction mirrors `Operation` for the
    same forward-compatibility reason.

    Attributes:
        path_params: Values substituted into the operation's ``{placeholder}``
            path tokens. Each value is percent-encoded as a single path segment
            by `assemble`.
        query: Query parameters appended after any query already present on the
            base URL.
        headers: Per-call headers overlaid on the operation's static headers,
            taking precedence per name.
        body: The optional request payload, carried through verbatim. This layer
            does not serialize the body — that is a later executor's concern.
    """

    path_params: Mapping[str, str] = field(default_factory=dict)
    query: QueryParams = field(default_factory=QueryParams)
    headers: Headers = field(default_factory=Headers)
    body: RequestBody | None = None
