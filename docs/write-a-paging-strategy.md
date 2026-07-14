# Writing a pagination strategy

A `PaginationStrategy` teaches the paginator one API's pagination convention:
given a decoded response, it returns a `Page` — the items on this page plus the
request that reaches the next page. The paginator does the I/O (each page fetch
runs through the full pipeline, so retry, auth, redirect, and tracing apply to
every page); the strategy is a pure translation with no side effects.

`core` ships three that cover the common conventions — `CursorStrategy`,
`PageNumberStrategy`, and `LinkHeaderStrategy`. You write your own when an API
paginates in a way none of them fit.

## The seam

```python
from __future__ import annotations

from dexpace.sdk.core.http.request import Request
from dexpace.sdk.core.pagination import HasHeaders, Page, PaginationStrategy
```

`PaginationStrategy[T]` has one method:

```python
def parse(
    self,
    response: HasHeaders,
    payload: object,
    template_request: Request,
) -> Page[T]: ...
```

- `response` is a header-only view of the response (for reading a `Link` header,
  say); its body is already decoded into `payload`.
- `payload` is the decoded body — usually a `dict` or `list`, or `None` for an
  empty body.
- `template_request` is the request that produced this page; you derive the next
  page's request from it so the auth headers, path, and method survive.

A `Page` carries `items`, a `next_request` (`None` at the end of the sequence),
an optional `prev_request`, and `raw` (the originating response, kept so the
page closes the connection deterministically).

## Prefer composing over a built-in

The wire-exact next-request splice is the subtle part: replacing one query
parameter must preserve every untouched parameter verbatim, byte for byte, with
no lossy `parse_qs`/`urlencode` round-trip (**PAGE-21**). The built-in
strategies already do this correctly, so the strongest pattern — and the one the
generated petstore client uses — is to delegate the page mechanics to a built-in
and only add your decode on top:

```python
from __future__ import annotations

from dataclasses import dataclass

from dexpace.sdk.core.http.request import Request
from dexpace.sdk.core.pagination import CursorStrategy, HasHeaders, Page, PaginationStrategy


@dataclass(frozen=True, slots=True)
class TypedCursorStrategy:
    """CursorStrategy plus a per-item decode into a typed model."""

    inner: CursorStrategy[object]

    def parse(self, response: HasHeaders, payload: object, template_request: Request) -> Page[str]:
        page = self.inner.parse(response, payload, template_request)
        names = [str(item) for item in page.items]  # decode raw items into your type here
        return Page(
            items=names,
            next_request=page.next_request,  # the wire-exact splice carries through
            prev_request=page.prev_request,
            raw=page.raw,
        )


strategy: PaginationStrategy[str] = TypedCursorStrategy(
    CursorStrategy(items_field="data", cursor_response_field="next_cursor", cursor_param="cursor")
)
```

`LinkHeaderStrategy` follows an RFC 8288 `Link` `rel="next"` target
(**PAGINATION-06**), folding a header split across multiple lines before
parsing; `PageNumberStrategy` increments a page-index parameter. Reach for a
from-scratch `parse` only when your API fits none of these.

## Contract checklist

- **Purity.** `parse` performs no I/O and holds no mutable per-iteration state,
  so one instance is reused verbatim by both the sync and async paginators. Keep
  it a pure function of its three inputs.
- **Wire-exact next request (PAGE-21).** Derive `next_request` from
  `template_request` so untouched query parameters, the path, headers, and body
  survive unchanged. Compose over a built-in for the query splice rather than
  re-encoding the query yourself.
- **Honest termination.** Return `next_request=None` the moment the sequence is
  exhausted (no cursor, an empty page, the last page index). A malformed or
  unparseable next target ends the walk by returning `None` rather than raising —
  one bad value must not abort a walk that has already yielded good pages.
- **`Link` following (PAGINATION-06)**, if your convention is header-driven:
  resolve a relative target against the template URL, and fold a multi-line
  `Link` header before parsing.
- **Carry `raw`.** Pass the originating response through as `Page(raw=...)` so
  the paginator can close the underlying connection.

## Certifying your strategy

Run the pagination conformance suite, which drives the strategies through the
paginator and checks item order, termination, and the wire-exact splice against
the golden `pagination_splice` corpus (**PAGE-21**) and the `Link`-header
vectors (**PAGINATION-06**):

```bash
pytest packages/dexpace-sdk-core/tests/pagination/
```

Add a parametrized case for your strategy alongside the built-ins, or point the
splice vectors at it, so its next-request derivation is held to the same
wire-exactness bar.
