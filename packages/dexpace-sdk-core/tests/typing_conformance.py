# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Static typing-conformance corpus for the public API surface.

Every function here is a compile-time-only assertion: it takes already-typed
values as parameters and uses `typing.assert_type` to pin the *exact* static
type the public API promises. The functions are deliberately never called at
run time — importing the module is a no-op — so the corpus exercises the type
checker, not the interpreter.

Two checkers consume this file:

- `mypy --strict` type-checks it as part of the normal (blocking) gate. A
  regression that widens, narrows, or drops a public return type turns an
  `assert_type` into a hard error here.
- `pyright` can run over the same corpus in a non-blocking lane as a
  cross-checker early-warning signal (see the CI ``typecheck-divergence`` job).

A failure means a public signature changed. Fix the source, or — if the change
is intentional — update the assertion here in the same commit so both checkers
agree on the new contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import BinaryIO, assert_type

from dexpace.sdk.core.client import AsyncHttpClient, HttpClient
from dexpace.sdk.core.http.common import Headers
from dexpace.sdk.core.http.request import Request, RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.pagination import HasHeaders, Page, PaginationStrategy
from dexpace.sdk.core.serde import JSON_SERDE, Deserializer, Serde, Serializer


class Pet:
    """Stand-in model type used to track a generic parameter through the API."""

    name: str


def _response_predicates(response: Response) -> None:
    """`Response` status predicates each stay strictly `bool`."""
    assert_type(response.is_success, bool)
    assert_type(response.is_redirect, bool)
    assert_type(response.is_client_error, bool)
    assert_type(response.is_server_error, bool)


def _response_with_helpers(response: Response, status: Status, headers: Headers) -> None:
    """The `with_*` helpers return the concrete `Response`, not a bare `Self`."""
    assert_type(response.with_status(status), Response)
    assert_type(response.with_headers(headers), Response)
    assert_type(response.with_header("x-trace", "abc"), Response)
    assert_type(response.with_body(None), Response)


def _http_client_execute(client: HttpClient, request: Request) -> None:
    """The sync transport seam maps one `Request` to one `Response`."""
    assert_type(client.execute(request), Response)


async def _async_http_client_execute(client: AsyncHttpClient, request: Request) -> None:
    """The async transport seam awaits to an `AsyncResponse`."""
    assert_type(await client.execute(request), AsyncResponse)


def _serde_surface(serde: Serde, value: object, stream: BinaryIO) -> None:
    """`Serde` exposes typed serializer / deserializer halves."""
    assert_type(serde.serializer, Serializer)
    assert_type(serde.deserializer, Deserializer)
    assert_type(serde.serializer.serialize(value), str)
    assert_type(serde.serializer.serialize_to_bytes(value), bytes)
    assert_type(serde.serializer.serialize_to_stream(value, stream), None)


def _json_serde_surface() -> None:
    """The shipped JSON `Serde` instance honours the same static contract."""
    assert_type(JSON_SERDE.serializer.serialize({"a": 1}), str)
    assert_type(JSON_SERDE.serializer.serialize_to_bytes({"a": 1}), bytes)


def _pagination_generic_flow(
    strategy: PaginationStrategy[Pet], response: HasHeaders, request: Request
) -> None:
    """A `PaginationStrategy[Pet]` threads `Pet` through to `Page[Pet]`.

    This is the generic-parametrisation check: the element type declared on the
    strategy flows all the way to ``page.items`` without erasing to ``object``.
    """
    page = strategy.parse(response, {}, request)
    assert_type(page, Page[Pet])
    assert_type(page.items, Sequence[Pet])
    assert_type(page.has_next, bool)
    assert_type(page.next_request, Request | None)


def _headers_lookup(headers: Headers) -> None:
    """`Headers.get` is optional-valued; membership is a plain `bool`."""
    assert_type(headers.get("content-type"), str | None)
    assert_type("content-type" in headers, bool)


def _request_body_factories() -> None:
    """The `RequestBody` factories return the base `RequestBody` type."""
    assert_type(RequestBody.from_bytes(b"payload"), RequestBody)
    assert_type(RequestBody.from_string("payload"), RequestBody)
