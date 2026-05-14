# Dexpace Python SDK

[![CI](https://github.com/dexpace/python-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/dexpace/python-sdk/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Type-checked: mypy --strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy.readthedocs.io/)
[![Ruff](https://img.shields.io/badge/lint-ruff-blue.svg)](https://docs.astral.sh/ruff/)

> [!CAUTION]
> **Proprietary & confidential software. No rights are granted.** Use,
> copying, modification, or distribution without explicit written consent
> from **Omar Aljarrah / dexpace** constitutes copyright infringement.

A toolkit for building Python HTTP client libraries — immutable models, a
stage-based pipeline runtime, pluggable transports, and an authentication
pillar including OAuth bearer tokens and RFC 7616 Digest. Targets Python
3.12+, ships strict type annotations (`mypy --strict`), and keeps the core
distribution dependency-free.

The SDK is **not** an HTTP client. It defines the contracts (`HttpClient`,
`AsyncHttpClient`) and supplies models, policies, and observability hooks;
transport packages bring the actual networking. Pick the transport that
matches your dependency constraints (or write your own — the Protocol is
six lines).

## Packages

The repository is a [`uv`](https://docs.astral.sh/uv/)-managed workspace of
five distributions sharing the `dexpace.sdk.*` namespace:

| Package                          | Purpose                                                           | Runtime dependencies        |
|----------------------------------|-------------------------------------------------------------------|-----------------------------|
| `dexpace-sdk-core`               | Toolkit: models, pipeline, policies, auth, observability          | None (stdlib only)          |
| `dexpace-sdk-http-stdlib`        | `UrllibHttpClient` (sync) + `AsyncioHttpClient` (async)           | `dexpace-sdk-core`          |
| `dexpace-sdk-http-httpx`         | `HttpxHttpClient` (sync) + `AsyncHttpxHttpClient`                 | `httpx`                     |
| `dexpace-sdk-http-aiohttp`       | `AiohttpHttpClient` (async only)                                  | `aiohttp`                   |
| `dexpace-sdk-http-requests`      | `RequestsHttpClient` (sync only)                                  | `requests`                  |

End-users install only the transport they need:

```bash
pip install dexpace-sdk-core dexpace-sdk-http-httpx
```

## What's in `dexpace-sdk-core`

| Subpackage                      | Surface                                                                                                  |
|---------------------------------|----------------------------------------------------------------------------------------------------------|
| `http.request`                  | `Request`, `RequestBody`, `FileRequestBody`, `LoggableRequestBody`, `MultipartRequestBody`, `Method`     |
| `http.response`                 | `Response`, `AsyncResponse`, `ResponseBody`, `AsyncResponseBody`, `LoggableResponseBody`, `Status`       |
| `http.common`                   | `Headers`, `HttpHeaderName`, `MediaType`, `Protocol`, `Url`, `QueryParams`, `ETag`, `HttpRange`, `RequestConditions`, paging primitives |
| `http.context`                  | `CallContext` → `DispatchContext` → `RequestContext` → `ExchangeContext` chain, `ContextStore`           |
| `http.auth`                     | `BearerTokenPolicy`, `BasicAuthPolicy`, `KeyCredentialPolicy`, `DigestChallengeHandler`, RFC 7235 challenge parser, `TokenCache` |
| `http.sse`                      | `SseParser` for Server-Sent Events streams                                                               |
| `pipeline`                      | `Pipeline`, `AsyncPipeline`, `Policy` ABC, `Stage` enum, `StagedPipelineBuilder`, `default_pipeline()`   |
| `pipeline.policies`             | `RetryPolicy`, `RedirectPolicy`, `SetDatePolicy`, `LoggingPolicy`, `TracingPolicy` (+ async twins)       |
| `client`                        | `HttpClient` and `AsyncHttpClient` Protocols                                                             |
| `serde`                         | `Serde`, `Serializer`, `Deserializer` Protocols + `JsonSerde` reference impl                             |
| `instrumentation`               | `ClientLogger`, `UrlRedactor`, `Tracer`, `Span`, `InstrumentationContext`, noop singletons               |
| `errors`                        | `SdkError` hierarchy: `ServiceRequestError`, `ServiceResponseError`, `HttpResponseError[ModelT]`, …      |
| `util`                          | `Clock`, `AsyncClock`, `ProxyOptions`                                                                    |
| `config`                        | `Configuration` (layered env-var + override lookup) + `ConfigurationBuilder`                             |

## Quick start

### Workspace setup (contributors)

```bash
git clone https://github.com/dexpace/python-sdk.git
cd python-sdk
uv sync
```

`uv sync` provisions a virtualenv with every workspace package installed in
editable mode plus the dev toolchain (`pytest`, `mypy`, `ruff`). All
commands below run via `uv run …`.

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
canonical policy stack (redirect → retry → set-date → logging → tracing).
Add authentication and customise as needed:

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

`StagedPipelineBuilder` enforces stage-based ordering and pillar
constraints (one redirect policy, one retry policy, one auth policy)
through type-checked surgical edits — `replace`, `insert_after`,
`insert_before`, `remove` operate by policy class.

### Streaming and replayable bodies

```python
from dexpace.sdk.core.http.request import RequestBody

# Single-use streams
RequestBody.from_stream(open("payload.bin", "rb"))
RequestBody.from_iter([b"chunk-1", b"chunk-2"])

# Replayable (transport may use zero-copy sendfile)
RequestBody.from_file("upload.bin")

# Convert any single-use body into a replayable one before retrying
replayable = body.to_replayable()
```

`RetryPolicy` automatically buffers single-use bodies before the first
attempt when retries are configured.

## Architecture

The pipeline runs request flow through ordered `Policy` instances; each
policy can mutate the request, invoke the downstream chain, and
post-process the response. The terminal `Policy` is wired to an
`HttpClient` transport.

```
caller → Pipeline → REDIRECT → RETRY → SET_DATE → AUTH → LOGGING → POST_LOGGING → HttpClient → wire
                     (pillar)  (pillar)            (pillar) (pillar)
```

Stage ordering is enforced by `Stage`, an `IntEnum` with sparse 100-apart
values that leave room for new stages without renumbering. Pillar stages
admit at most one policy; non-pillar stages stack with deque semantics. The
list-form `Pipeline(client, policies=[...])` constructor remains available
for callers who want explicit ordering.

Layered, bottom-up:

1. **Bodies.** `RequestBody.iter_bytes(chunk)` produces bytes on demand;
   `ResponseBody.iter_bytes` / `bytes()` / `string()` consume them.
   Stream-backed variants are single-use; bytes-backed and file-backed
   variants are replayable.
2. **Models.** `Request`, `Response`, `Headers`, `Url`, etc. are
   `@dataclass(frozen=True, slots=True)`. Mutate via `dataclasses.replace`
   or `with_*` helpers.
3. **Context.** `DispatchContext` → `RequestContext` → `ExchangeContext`
   carry the `InstrumentationContext` (trace id, span id, flags) and are
   registered in the thread-safe `ContextStore` keyed by trace id.
4. **Pipeline.** `Policy` ABC + `Stage` enum. Policies declare
   `STAGE: ClassVar[Stage]` (enforced at class-creation via
   `__init_subclass__`) and slot into `StagedPipelineBuilder` accordingly.
5. **Client.** `HttpClient.execute(request) → Response` and its async twin
   are the only transport contracts. Concrete transports live in separate
   distributions.

## Highlights

- **Strictly typed.** `mypy --strict` clean; no `Any` in public API; PEP
  695 type parameters where they fit; `Literal[Stage.X]` to lock policy
  stages at the type level.
- **Immutable data with slots.** Frozen dataclasses with `__slots__` for
  every model; non-destructive updates via `with_*` helpers.
- **Pluggable everything.** `HttpClient` / `AsyncHttpClient` /
  `ChallengeHandler` / `Serde` / `TokenCache` / `Clock` /
  `Configuration` are all duck-typed Protocols.
- **Real auth.** OAuth bearer with concurrent-refresh serialization,
  `WWW-Authenticate` challenge parser per RFC 7235, RFC 7616 Digest
  (MD5 / MD5-sess / SHA-256 / SHA-256-sess), basic, and key credential.
- **Retry done right.** Exponential backoff with jitter, `Retry-After`
  awareness, automatic single-use-body replay, deterministic via
  injectable `Clock`.
- **Redirects done right.** Loop detection, `Authorization` stripping on
  reissue, userinfo dropped from `Location` URLs, configurable allowed
  methods and 303 handling.
- **Observability.** Structured logging via `LoggingPolicy`, OpenTelemetry-
  compatible spans via `TracingPolicy`, URL redaction with allowlisted
  query parameters, body capture caps for diagnostic logging.
- **Server-Sent Events.** WHATWG-compliant `SseParser` with bounded line
  buffer.
- **Zero core dependencies.** `dexpace-sdk-core` ships against the
  standard library only; transport adapters bring exactly one third-party
  HTTP library each.

## Development

```bash
uv sync                          # provision workspace + dev tools
uv run pytest -q                 # 646 tests across 5 packages
uv run mypy --strict             # type-check (171 source files)
uv run ruff check                # lint
uv run ruff format --check       # formatting gate
```

CI runs the same gates on Python 3.12 and 3.13 for every push and pull
request — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Conventions

Documented in [`CLAUDE.md`](CLAUDE.md). Highlights:

- Python 3.12+; modern syntax (`X | None`, `list[X]`, `Self`, PEP 695
  generics); `from __future__ import annotations` everywhere.
- Frozen dataclasses with slots; no builder objects.
- `Protocol` for duck-typed SPIs, `ABC` for shared default behaviour.
- Context managers for resources (`Response`, `ResponseBody`,
  `CallContext`).
- `mypy --strict` and `ruff` clean on every commit; Google-style
  docstrings on every public symbol; function-size cap of 50 lines.
