# Dexpace Python SDK

> [!CAUTION]
> **PROPRIETARY & CONFIDENTIAL SOFTWARE**
>
> **NO RIGHTS ARE GRANTED.**
>
> Any use, copying, modification, or distribution without
> explicit written consent from **Omar Aljarrah / dexpace**
> constitutes copyright infringement.

A zero-dependency, production-grade SDK core for building and maintaining Python HTTP
client libraries. Pure standard library, targeting **Python 3.12+**, with immutable
HTTP models, Pythonic body abstractions, and a composable pipeline architecture — the
Python counterpart to [`dexpace/java-sdk`](https://github.com/dexpace/java-sdk).

## Highlights

- **Zero runtime dependencies** — standard library only.
- **Immutable HTTP models** — `Request`, `Response`, `Headers`, `MediaType`,
  `Protocol`, `Url`, `ETag`, `HttpRange`, `RequestConditions` are frozen
  `@dataclass(frozen=True, slots=True)`; non-destructive mutation via
  `dataclasses.replace` and `with_*` helpers.
- **Typed body abstractions** — `RequestBody` factories (`from_bytes`,
  `from_string`, `from_form`, `from_stream`, `from_iter`, `from_file`) and
  `ResponseBody` factories (`from_bytes`, `from_stream`) cover the common
  shapes; `iter_bytes` for streaming, `bytes()` / `string()` for full reads.
- **Body capture for logging** — `LoggableRequestBody` mirrors writes into an
  in-memory tap via `BytesIO`; `LoggableResponseBody` caches the body for
  repeatable reads. Both honour a configurable byte cap.
- **Pipeline architecture** — `PipelineStep[T, V]` as a `Protocol`, composable
  into request / response chains. `RetryableStep` adds a retry hook.
- **Context promotion chain** — `DispatchContext` → `RequestContext` →
  `ExchangeContext`, each carrying an `InstrumentationContext` for tracing
  correlation. The thread-safe `ContextStore` is keyed by trace id.
- **`Protocol`-first SPI** — `HttpClient`, `Serde`, `Serializer`,
  `Deserializer` are structural; any object with the right shape qualifies.
- **`mypy --strict` clean** — every public signature is typed; modern union
  syntax (`X | None`), built-in generics (`list[X]`), and PEP 695 type
  parameters where they fit.

## Project Structure

```
python-sdk/
  src/
    dexpace/sdk/core/        Single package — all SDK core code
      http/                  Request, Response, Headers, MediaType, Url, ETag, …
      pipeline/              PipelineStep + step config
      client/                HttpClient Protocol
      serde/                 Serde, Serializer, Deserializer
      instrumentation/       InstrumentationContext, Span, TracingScope (noops included)
  tests/                     pytest suite
  pyproject.toml
```

### Subpackages (`dexpace.sdk.core`)

| Package                                                     | Description                                                                                       |
|-------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`http.request`](src/dexpace/sdk/core/http/request)         | Immutable `Request`, `RequestBody`, `FileRequestBody`, `LoggableRequestBody`, `Method`            |
| [`http.response`](src/dexpace/sdk/core/http/response)       | Immutable `Response`, `ResponseBody`, `LoggableResponseBody`, `Status`                            |
| [`http.common`](src/dexpace/sdk/core/http/common)           | `Headers`, `HttpHeaderName`, `MediaType`, `Protocol`, `Url`, `QueryParams`, `ETag`, `HttpRange`, `RequestConditions`, `common_media_types` |
| [`http.context`](src/dexpace/sdk/core/http/context)         | `CallContext`, `DispatchContext`, `RequestContext`, `ExchangeContext`, `ContextStore`             |
| [`pipeline`](src/dexpace/sdk/core/pipeline)                 | `PipelineStep`, `RequestPipelineStep`, `ResponsePipelineStep`, `StepMetadata`, `RetryConfig`      |
| [`client`](src/dexpace/sdk/core/client)                     | `HttpClient` Protocol                                                                             |
| [`serde`](src/dexpace/sdk/core/serde)                       | `Serde`, `Serializer`, `Deserializer` Protocols                                                   |
| [`instrumentation`](src/dexpace/sdk/core/instrumentation)   | `InstrumentationContext`, `Span`, `Tracer`, `TracingScope`, noop singletons                       |

## Quick Start

### Building a request

Models are frozen dataclasses — construct directly and mutate via `with_*`
helpers or `dataclasses.replace`:

```python
from dexpace.sdk.core.http.common import Headers, common_media_types
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE, USER_AGENT
from dexpace.sdk.core.http.request import Method, Request, RequestBody

request = Request(
    method=Method.POST,
    url="https://api.example.com/v1/resource",
    headers=Headers([(CONTENT_TYPE, "application/json")]),
    body=RequestBody.from_string(
        '{"key": "value"}',
        media_type=common_media_types.APPLICATION_JSON,
    ),
)

# Non-destructive updates return a new Request:
retried = request.with_added_header("X-Retry-Count", "1").with_header(USER_AGENT, "my-app/1.0")
```

### Consuming a response

`Response` and `ResponseBody` are context managers — use `with` to release the
transport handle deterministically:

```python
with http_client.execute(request) as response:
    if response.is_success:
        text = response.body.string()        # decodes per media-type charset
        # … or `.bytes()` for raw bytes, or `iter_bytes(chunk_size)` to stream
```

### Streaming bodies

```python
from io import BytesIO
from dexpace.sdk.core.http.request import RequestBody

# from an open stream (single-use)
body = RequestBody.from_stream(open("/path/to/payload", "rb"))

# from an iterable of byte chunks (single-use)
body = RequestBody.from_iter([b"chunk-1", b"chunk-2"])

# from a file on disk (replayable, transport may use zero-copy sendfile)
body = RequestBody.from_file("/path/to/upload.bin")
```

### Capturing a body for logging

```python
from dexpace.sdk.core.http.request import LoggableRequestBody, RequestBody

inner = RequestBody.from_string("payload")
logged = LoggableRequestBody(inner, max_capture_bytes=8 * 1024)

# Drain to a transport sink; bytes are mirrored into the in-memory tap.
logged.write_to(some_binary_stream)

print("captured:", logged.snapshot()[:200])
```

### Writing a pipeline step

`PipelineStep[T_in, T_out]` is a `Protocol` — any callable with the matching
shape qualifies, including plain functions and lambdas:

```python
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Request
from dexpace.sdk.core.pipeline import PipelineStep


def add_user_agent(request: Request, context: DispatchContext) -> Request:
    return request.with_header("User-Agent", "my-app/1.0")


step: PipelineStep[Request, Request] = add_user_agent
```

## Architecture

The SDK is an **HTTP-client toolkit, not an HTTP client**. It provides
abstractions, models, and pipelines; consuming libraries plug in a concrete
transport via the `HttpClient` Protocol.

Layered, bottom-up:

1. **Bodies** — `RequestBody` produces bytes on demand via `iter_bytes`;
   `ResponseBody` exposes `iter_bytes` / `bytes` / `string`. Stream-backed
   variants are single-use; bytes-backed and file-backed variants are
   replayable. The `Loggable*` decorators wrap either side for diagnostic
   capture with a configurable cap.
2. **`http.request` / `http.response` / `http.common`** — immutable
   frozen-dataclass models with `slots=True`. Non-destructive mutation via
   `dataclasses.replace` or the `with_*` helpers.
3. **`http.context`** — promotion chain `DispatchContext` → `RequestContext`
   → `ExchangeContext`, all carrying an `InstrumentationContext` for tracing
   correlation, registered in the thread-safe `ContextStore` by trace id.
4. **`pipeline/`** — `PipelineStep[T_in, T_out]` Protocol is the building
   block; `RetryableStep` adds a retry hook (this Protocol is provisional;
   M2 will replace it with a `Policy` ABC that wraps the downstream chain).
   `StepMetadata` and `RetryConfig` provide optional configuration objects.
5. **`client/HttpClient`** — single-method Protocol
   (`execute(request) -> Response`). Transport is **not** provided by `core`.

## Conventions

- **Python 3.12+.** Modern syntax: `X | None`, `list[X]`, `dict[X, Y]`,
  `Self`, PEP 695 type parameters (`def f[T](x: T) -> T`). `from __future__
  import annotations` everywhere.
- **Immutable data, no builders.** Models are
  `@dataclass(frozen=True, slots=True)`; mutate via `dataclasses.replace` or
  the `with_*` helpers. Builders are a Java idiom — Python's
  keyword/default arguments make them redundant.
- **Thread-safety where stated.** `ContextStore` is safe under concurrent
  calls; individual bodies and streams are not.
- **`Protocol` over `ABC`.** Structural Protocols for SPIs (`HttpClient`,
  `Serde`, `PipelineStep`); ABCs only where shared default behaviour is
  necessary (`RequestBody`, `ResponseBody`, `Span`, `TracingScope`).
- **`mypy --strict` and `ruff` clean** on every commit.
- **No runtime dependencies.** Add nothing to `pyproject.toml` beyond stdlib.
- **Google-style docstrings** on every public symbol — one-line summary,
  blank line, then `Args:` / `Returns:` / `Raises:` / `Yields:` sections.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
ruff format src tests
mypy
pytest
```

## Tech Stack

| Component    | Version       |
|--------------|---------------|
| Python       | 3.12+         |
| Dependencies | None (stdlib) |
