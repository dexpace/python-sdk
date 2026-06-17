<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dexpace-wordmark-dark.svg">
    <img alt="dexpace" src="docs/assets/dexpace-wordmark-light.svg" width="320">
  </picture>
</p>

<h1 align="center">Dexpace Python SDK</h1>

[![CI](https://github.com/dexpace/python-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/dexpace/python-sdk/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Type-checked: mypy --strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy.readthedocs.io/)
[![Ruff](https://img.shields.io/badge/lint-ruff-blue.svg)](https://docs.astral.sh/ruff/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE.md)

A toolkit for building Python HTTP client libraries. It provides immutable
request and response models, a staged policy pipeline, pluggable transports,
and an authentication pillar that speaks OAuth bearer tokens and RFC 7616
Digest. Everything is typed end to end under `mypy --strict` and targets
Python 3.12 or later.

The SDK is deliberately not an HTTP client. It defines the contracts
(`HttpClient`, `AsyncHttpClient`) and supplies the models, policies, and
observability hooks that surround them; the networking itself arrives
through a transport package of your choosing. Pick the adapter that fits
your dependency budget, or write your own: the Protocol is six lines.

## Packages

The repository is a [`uv`](https://docs.astral.sh/uv/)-managed workspace of
five distributions sharing the `dexpace.sdk.*` namespace. Each transport
package depends on `dexpace-sdk-core` and exactly one HTTP library.

| Package                     | Provides                                                  | Third-party dependencies |
|-----------------------------|-----------------------------------------------------------|--------------------------|
| `dexpace-sdk-core`          | Models, pipeline, policies, auth, observability           | `furl`                   |
| `dexpace-sdk-http-stdlib`   | `UrllibHttpClient` (sync), `AsyncioHttpClient` (async)    | none (stdlib only)       |
| `dexpace-sdk-http-httpx`    | `HttpxHttpClient` (sync), `AsyncHttpxHttpClient` (async)  | `httpx`                  |
| `dexpace-sdk-http-aiohttp`  | `AiohttpHttpClient` (async)                               | `aiohttp`                |
| `dexpace-sdk-http-requests` | `RequestsHttpClient` (sync)                               | `requests`               |

Install the core plus whichever transport you need:

```bash
pip install dexpace-sdk-core dexpace-sdk-http-httpx
```

## Quick start

### A minimal request

```python
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.http.stdlib import UrllibHttpClient

request = Request(method=Method.GET, url=Url.parse("https://httpbin.org/get"))

with UrllibHttpClient() as client, client.execute(request) as response:
    print(response.status, response.body.string())
```

### A POST with a JSON body

```python
from dexpace.sdk.core.http.common import Headers, Url, common_media_types
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.http.stdlib import UrllibHttpClient

request = Request(
    method=Method.POST,
    url=Url.parse("https://httpbin.org/post"),
    headers=Headers([(CONTENT_TYPE, "application/json")]),
    body=RequestBody.from_string(
        '{"hello": "world"}',
        media_type=common_media_types.APPLICATION_JSON,
    ),
)

with UrllibHttpClient() as client, client.execute(request) as response:
    if response.is_success:
        print(response.body.string())
```

### A configured pipeline

`default_pipeline()` returns a `StagedPipelineBuilder` pre-wired with the
canonical policy stack (redirect, idempotency, retry, set-date,
client-identity, logging, tracing, operation-tracing). Add authentication and adjust whatever
the defaults get wrong for you:

```python
from dexpace.sdk.core.http.auth import BearerTokenPolicy
from dexpace.sdk.core.pipeline import default_pipeline
from dexpace.sdk.core.pipeline.policies import RetryPolicy
from dexpace.sdk.http.httpx import HttpxHttpClient

pipeline = default_pipeline(
    HttpxHttpClient(),
    retry=RetryPolicy(total_retries=5),
    auth=BearerTokenPolicy(my_credential, "https://api.example.com/.default"),
).build()

with pipeline:
    response = pipeline.run(request, dispatch_context)
```

The builder enforces stage ordering and pillar constraints (one redirect
policy, one retry policy, one auth policy) and supports surgical,
type-checked edits: `replace`, `insert_after`, `insert_before`, and
`remove` all operate by policy class.

### Streaming and replayable bodies

```python
from dexpace.sdk.core.http.request import RequestBody

# Single-use streams
RequestBody.from_stream(open("payload.bin", "rb"))
RequestBody.from_iter([b"chunk-1", b"chunk-2"])

# Replayable; a transport could special-case file bodies (e.g. zero-copy
# sendfile), though none of the shipped transports do so today
RequestBody.from_file("upload.bin")

# Convert any single-use body into a replayable one before retrying
replayable = body.to_replayable()
```

When retries are configured, `RetryPolicy` buffers single-use bodies
before the first attempt so a retry never finds the stream already drained.

## Architecture

A request flows through ordered `Policy` instances. Each policy may rewrite
the request, invoke the chain below it, and post-process the response on
the way back up. The terminal policy hands the request to an `HttpClient`
transport.

```
caller → Pipeline → OPERATION → REDIRECT → POST_REDIRECT → RETRY → POST_RETRY → [AUTH] → LOGGING → POST_LOGGING → HttpClient → wire
                    (pillar)     (pillar)   idempotency    (pillar)  set-date    (pillar) (pillar)  tracing
                                                                      client-identity
```

Ordering is governed by `Stage`, an `IntEnum` whose values are spaced out so
new stages can land without renumbering. Pillar stages admit a single
policy; non-pillar stages stack with deque semantics. Callers who prefer
explicit ordering can still use the list form,
`Pipeline(client, policies=[...])`.

Bottom-up, the layers are:

1. **Bodies.** `RequestBody.iter_bytes(chunk)` produces bytes on demand;
   `ResponseBody.iter_bytes`, `bytes()`, and `string()` consume them.
   Stream-backed variants are single-use; bytes-backed and file-backed
   variants replay freely.
2. **Models.** `Request`, `Response`, `Headers`, `Url`, and their kin are
   `@dataclass(frozen=True, slots=True)`. Mutation happens through
   `dataclasses.replace` or the `with_*` helpers, never in place.
3. **Context.** `DispatchContext` promotes to `RequestContext` and then
   `ExchangeContext`, carrying an `InstrumentationContext` (trace id, span
   id, flags) throughout. The thread-safe `ContextStore` indexes live
   contexts by trace id.
4. **Pipeline.** The `Policy` ABC pairs with the `Stage` enum. Each policy
   declares `STAGE: ClassVar[Stage]`, checked at class creation via
   `__init_subclass__`, and slots into the builder accordingly.
5. **Client.** `HttpClient.execute(request) → Response` and its async twin
   are the only transport contracts. Concrete transports live in their own
   distributions.

## Inside the core

| Subpackage          | Surface                                                                                                  |
|---------------------|----------------------------------------------------------------------------------------------------------|
| `http.request`      | `Request`, `RequestBody`, `FileRequestBody`, `LoggableRequestBody`, `MultipartRequestBody`, `Method`     |
| `http.response`     | `Response`, `AsyncResponse`, `ResponseBody`, `AsyncResponseBody`, `LoggableResponseBody`, `Status`       |
| `http.common`       | `Headers`, `HttpHeaderName`, `MediaType`, `Protocol`, `Url`, `QueryParams`, `ETag`, `HttpRange`, `RequestConditions`, paging primitives |
| `http.context`      | `CallContext` → `DispatchContext` → `RequestContext` → `ExchangeContext` chain, `ContextStore`           |
| `http.auth`         | `BearerTokenPolicy`, `BasicAuthPolicy`, `KeyCredentialPolicy`, `DigestChallengeHandler`, RFC 7235 challenge parser, `TokenCache` |
| `http.sse`          | `SseParser`, plus reconnecting `SseConnection` / `AsyncSseConnection` (Last-Event-ID replay + backoff)   |
| `http.webhooks`     | `WebhookVerifier`, `InvalidWebhookSignatureError` — HMAC signature verification with timestamp tolerance  |
| `pagination`        | `Page`, `Paginator` / `AsyncPaginator`, `PaginationStrategy` (`CursorStrategy`, `PageNumberStrategy`, `LinkHeaderStrategy`) |
| `pipeline`          | `Pipeline`, `AsyncPipeline`, `Policy` ABC, `Stage` enum, `StagedPipelineBuilder`, `default_pipeline()`   |
| `pipeline.policies` | `RedirectPolicy`, `IdempotencyPolicy`, `RetryPolicy`, `SetDatePolicy`, `ClientIdentityPolicy`, `LoggingPolicy`, `OperationTracingPolicy`, `TracingPolicy` (async twins for all but `LoggingPolicy` and `TracingPolicy`) |
| `client`            | `HttpClient` and `AsyncHttpClient` Protocols                                                             |
| `serde`             | `Serde`, `Serializer`, `Deserializer` Protocols + `JsonSerde` reference impl                             |
| `instrumentation`   | `ClientLogger`, `UrlRedactor`, `Tracer`, `Span`, `InstrumentationContext`, `contextvars` correlation helpers, noop singletons |
| `errors`            | `SdkError` hierarchy: `ServiceRequestError`, `ServiceResponseError`, `HttpResponseError[ModelT]`, …      |
| `util`              | `Clock`, `AsyncClock`, `ProxyOptions`                                                                    |
| `config`            | `Configuration` (layered env-var + override lookup) + `ConfigurationBuilder`                             |

## Highlights

- **Strictly typed.** Clean under `mypy --strict`, with no `Any` in the
  public API. PEP 695 type parameters appear where they fit, and
  `Literal[Stage.X]` pins policy stages at the type level.
- **Immutable data with slots.** Every model is a frozen dataclass with
  `__slots__`; updates are non-destructive through `with_*` helpers.
- **Pluggable everything.** `HttpClient`, `AsyncHttpClient`,
  `ChallengeHandler`, `Serde`, `TokenCache`, `Clock`, and `Configuration`
  are all duck-typed Protocols, so a conforming class is a valid
  implementation with no registration step.
- **Real auth.** OAuth bearer with serialized concurrent refresh, a
  `WWW-Authenticate` challenge parser per RFC 7235, RFC 7616 Digest
  (MD5, MD5-sess, SHA-256, SHA-256-sess), basic, and key credential.
- **Retry done right.** Exponential backoff with jitter, `Retry-After`
  awareness, automatic replay of single-use bodies, and deterministic
  behaviour under test through an injectable `Clock`.
- **Redirects done right.** Loop detection, `Authorization` stripped on
  reissue, userinfo dropped from `Location` URLs, configurable allowed
  methods and 303 handling.
- **Observability.** Structured logging via `LoggingPolicy`,
  per-attempt OpenTelemetry spans via `TracingPolicy` with a once-per-call
  tracer lifecycle via `OperationTracingPolicy`, URL redaction with
  allowlisted query parameters, and capped body capture for diagnostics.
- **Server-Sent Events.** A WHATWG-compliant `SseParser` with a bounded
  line buffer, plus reconnecting `SseConnection` / `AsyncSseConnection`
  that resume with `Last-Event-ID` and honour server `retry:` backoff.
- **Pagination.** A top-level `pagination` package: `Page`, sync and async
  `Paginator`s that iterate item-by-item or page-by-page, and pluggable
  `PaginationStrategy` (cursor, page-number, and `Link`-header).
- **Webhooks.** `WebhookVerifier` checks HMAC signatures with a timestamp
  tolerance and constant-time comparison, raising
  `InvalidWebhookSignatureError` on mismatch.
- **Correlation.** `contextvars`-based trace/span propagation so the
  idempotency and client-identity policies and logging share one id.
- **A lean core.** `dexpace-sdk-core` carries a single runtime dependency
  (`furl`, which backs `Url` parsing); each transport adapter adds exactly
  one HTTP library.

## Development

The workspace is managed with `uv`; one sync provisions every package in
editable mode along with the dev toolchain (`pytest`, `mypy`, `ruff`).

```bash
git clone https://github.com/dexpace/python-sdk.git
cd python-sdk
uv sync
```

```bash
uv run pytest -q                 # run the full test suite across 5 packages
uv run mypy --strict             # type-check every package under strict mode
uv run ruff check                # lint
uv run ruff format --check       # formatting gate
```

CI runs the same gates on Python 3.12, 3.13, and 3.14 for every push and
pull request; see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Conventions

Documented in [`CLAUDE.md`](CLAUDE.md). The short version:

- Python 3.12+; modern syntax (`X | None`, `list[X]`, `Self`, PEP 695
  generics); `from __future__ import annotations` everywhere.
- Frozen dataclasses with slots; no builder objects.
- `Protocol` for duck-typed SPIs, `ABC` for shared default behaviour.
- Context managers for resources (`Response`, `ResponseBody`,
  `CallContext`).
- `mypy --strict` and `ruff` clean on every commit; Google-style
  docstrings on every public symbol; functions capped at 50 lines.

## Contributing

External pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for setup, the quality gates, and commit conventions, and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for community expectations.

## Security

To report a vulnerability, follow [`SECURITY.md`](SECURITY.md) — please do
not open a public issue.

## License

Released under the [MIT License](LICENSE.md).
Copyright © 2026 dexpace and Omar Aljarrah.
