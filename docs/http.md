# HTTP models

All HTTP models are immutable frozen dataclasses (`slots=True`).
Mutation is non-destructive: use `dataclasses.replace` or the `with_*`
helpers to derive a new instance.

## Request / Response

```python
from dexpace.sdk.core.http.common import Headers, Url
from dexpace.sdk.core.http.request import Method, Request

request = Request(
    method=Method.POST,
    url=Url.parse("https://api.example.com/v1/items"),
    headers=Headers({"Content-Type": "application/json"}),
)
updated = request.with_added_header("X-Trace", "abc")
```

## Headers

Case-insensitive, multi-valued, immutable. Pass `HttpHeaderName`
constants (`CONTENT_TYPE`, `AUTHORIZATION`, …) to skip the
`.lower()` step on the hot path:

```python
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE

headers = Headers([(CONTENT_TYPE, "application/json")])
assert headers.get(CONTENT_TYPE) == "application/json"
```

## MediaType / Url / ETag / HttpRange / RequestConditions

Each is a `@dataclass(frozen=True, slots=True)`. `MediaType`, `Url`, and
`ETag` expose a `parse(str)` classmethod and a `__str__` that round-trips.
`HttpRange` renders via `format()` / `to_header_value()`, and
`RequestConditions` applies itself onto a request via `apply_to(request)`:

```python
from dexpace.sdk.core.http.common import ETag, HttpRange, MediaType, Url

mt = MediaType.parse("application/json; charset=utf-8")
mt.charset                          # "utf-8"

url = Url.parse("https://api.example.com:8443/v1?a=1&b=2")
url.host, url.port                  # ("api.example.com", 8443)

tag = ETag(value="abc")
str(tag)                            # '"abc"'

HttpRange(0, 100).to_header_value() # "bytes=0-99"
```

`RequestConditions` bundles `If-Match` / `If-None-Match` /
`If-Modified-Since` / `If-Unmodified-Since` and applies them onto a
request:

```python
from datetime import UTC, datetime
from dexpace.sdk.core.http.common import ETag, RequestConditions

cond = RequestConditions(
    if_none_match=[ETag(value="abc")],
    if_modified_since=datetime(2024, 1, 1, tzinfo=UTC),
)
conditioned = cond.apply_to(request)
```

## Context promotion chain

Each call moves through three immutable contexts:

1. `DispatchContext` — carries the `InstrumentationContext`. Created
   before a request payload exists.
2. `RequestContext` — adds the outgoing `Request`. Produced by
   `DispatchContext.to_request_context(request)`.
3. `ExchangeContext` — adds the `Response`. Produced by
   `RequestContext.to_exchange_context(response)`.

Each promotion registers the new tier in the thread-safe `ContextStore`
keyed by trace id. Downstream observers (metrics, log emitters, span
finalisers) look up the latest tier via `ContextStore.get(trace_id)`.

Pipeline policies receive the *mutable* `PipelineContext`, which wraps
the immutable promotion-chain context plus a `data` scratchpad and an
`options` dict for caller-supplied per-call overrides.
