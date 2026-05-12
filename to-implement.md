# Python SDK — Implementation Plan

Snapshot of work remaining after M1 lands. Organised into milestones; items
within a milestone are roughly independent and can be parallelised.

The shipping surface today is **contracts + immutable HTTP models + typed
Pythonic body abstractions**. What's listed below is what turns it from
"scaffolding" into "SDK you'd actually build a client library on top of".

This plan was revised after a deep study of the **Azure SDK for Python**
(`sdk/core/corehttp`) — the patterns it has battle-tested over years inform
several of the larger decisions below. References like *azure:_retry.py* point
to specific files under `azure-sdk-for-python/sdk/core/corehttp/corehttp/`.

### Recent design corrections

- **Removed the Java-style `io/` module.** Python's stdlib (`bytes`,
  `bytearray`, `memoryview`, `BytesIO`, `BinaryIO`) covers the same surface
  Okio's `Source` / `Sink` / `Buffer` / `IoProvider` provides in JVM-land,
  without the abstraction cost. Bodies now use `iter_bytes(chunk_size)` and
  `BinaryIO` directly. See README and CLAUDE.md.
- **Python floor bumped to 3.12+.** Aligns with the styleguide; lets us use
  `X | None`, `list[X]`, `Self`, PEP 695 type parameters, `StrEnum`.
- **`mypy --strict` and `ruff` clean** are enforced from day one (config in
  `pyproject.toml`).
- **Google-style docstrings** on every public symbol (styleguide §14). The
  M1 surface is converted; older modules carry over until M2.

---

## Resolved design decisions

These were open questions in the previous revision. Locked in now (with the
reasoning) so M2+ can proceed without re-litigating them.

| Decision | Resolution | Reasoning |
|----------|-----------|-----------|
| **Sync vs async surface** | Two parallel hierarchies (`HttpClient` / `AsyncHttpClient`, `Pipeline` / `AsyncPipeline`, sync/async `RequestBody`/`ResponseBody`). | Mirrors `httpx.Client`/`httpx.AsyncClient` and Azure's `Pipeline`/`AsyncPipeline`. SansIO policies are shared. |
| **Lint / format toolchain** | `ruff` + `ruff format`, no black. | Single tool, single config. |
| **Type-checking strictness** | `mypy --strict` from day one. | Surface is small; loose settings invite drift. |
| **Minimum Python** | 3.12+. | Styleguide §README target. Lets us use `X \| None`, `Self`, `StrEnum`, PEP 695 generics, etc. |
| **Test framework** | pytest + `pytest-asyncio` (when async lands). | Lower-friction fixtures + parametrise. |
| **Pipeline step model** | **Two-tier**: keep `PipelineStep` Protocol (SansIO — stateless `(value, ctx) → value` transforms, no `.next`, works in both sync and async pipelines) AND add a `Policy` ABC with `.next: Policy` for steps that need to wrap the downstream chain (retry, auth, tracing-span). Pipeline engine wraps a SansIO step in a `_SansIORunner` that calls `self.next` after the SansIO transform. | This is Azure's `SansIOHTTPPolicy` + `HTTPPolicy` split. The SansIO form is enough for 80% of cases (header stamps, redaction, logging) and is trivially reusable across sync/async. The `.next`-chained form is what makes retry and 401-challenge-on-bearer-auth expressible at all. The current `RetryableStep` Protocol with its separate `.retry()` hook is the wrong abstraction (you can't express "retry the *rest of the chain*" with it) — drop it in M2 (see note below). |
| **Context model** | Keep the existing `DispatchContext` → `RequestContext` → `ExchangeContext` promotion chain. Add a `data: Dict[str, Any]` slot on `ExchangeContext` for policy-managed state (retry counters, auth challenge state) à la Azure's `PipelineContext` dict. Keep the `options: Dict[str, Any]` distinction (caller-provided per-call overrides, vs `data` for policy bookkeeping). | Avoids the dict-subclass gymnastics of Azure's `PipelineContext` while keeping the same affordances. Our context already has the right shape for trace correlation; just give policies a place to stash mutable state without polluting the typed fields. |
| **Transport-aware sleep** | `HttpClient` Protocol gets a `sleep(duration: float) -> None` method (default impl `time.sleep`). The async twin uses `asyncio.sleep`. | From azure:_base.py — retry policies should ask the transport to sleep so an async transport can stay non-blocking. |

### Carry-over: drop the existing `RetryableStep`

The Protocol at `pipeline/step/pipeline_step.py:RetryableStep` is replaced by
the `Policy` ABC in M2. It was an early guess at the shape and the Azure study
confirms the better model is "retry is a policy that wraps the downstream
chain", not "a step that exposes a separate retry method".

---

## M1 — Foundation hardening ✅

Lock the existing surface in with tests and the small body shapes that the
Java SDK ships but the Python SDK omits. **Landed** alongside the styleguide
migration and the `io/` rip-out.

- [x] **pytest test suite** covering every contract under `tests/`:
  `tests/http/` (Headers, MediaType, HttpHeaderName, Url/QueryParams, ETag,
  HttpRange, RequestConditions, Request, RequestBody, FileRequestBody,
  LoggableRequestBody, Response, ResponseBody, LoggableResponseBody,
  Method/Status/Protocol round-trips); `tests/context/` (promotion chain,
  store evict, duplicate-id rejection). 145 tests, `pytest` clean.
- [ ] **GitHub Actions CI** — `ci.yml` matrix of Python 3.12 / 3.13 running
      `ruff check`, `ruff format --check`, `mypy --strict`, `pytest --cov`.
      Coverage gate at 90% for `src/dexpace/sdk/core/`. *(Deferred: no
      remote yet.)*
- [x] **`pyproject.toml` dev extras** — `pytest`, `pytest-cov`,
      `pytest-asyncio`, `ruff`, `mypy`. Ruff `select` includes `E,W,F,I,N,B,UP,SIM,RUF`;
      mypy strict; pytest async-mode auto.
- [x] **`py.typed` marker** at `src/dexpace/sdk/core/py.typed` (PEP 561).
- [x] **`LoggableRequestBody`** at
      `http/request/loggable_request_body.py` — wraps a `RequestBody`,
      mirrors bytes through `iter_bytes` into an in-memory `BytesIO` tap
      capped at `max_capture_bytes`; `snapshot()` returns the captured
      bytes.
- [x] **`LoggableResponseBody`** at
      `http/response/loggable_response_body.py` — wraps a `ResponseBody`,
      eagerly drains into a cached `bytes` on first access; subsequent
      `iter_bytes` calls replay the cache. Cap semantics mirror the
      request side.
- [x] **`FileRequestBody`** at `http/request/file_request_body.py` —
      replayable body backed by a `pathlib.Path`. Transports can
      `isinstance`-check to dispatch zero-copy `os.sendfile(2)`.
      `RequestBody.from_file(path, media_type=None, offset=0, count=-1)`
      factory is attached dynamically.
- [x] **`HttpHeaderName` typed constants** in
      `http/common/http_header_name.py` — frozen-dataclass wrapper carrying
      `(value, canonical_name)`; ships ~55 IANA-registered names.
      `Headers.get` / `with_added` / `with_set` / `without` accept both
      `str` and `HttpHeaderName`.
- [x] **URL helpers** at `http/common/url.py` — `Url(scheme, host, path, port,
      query, fragment, userinfo)` frozen dataclass with `parse(str)` and
      `__str__`; `QueryParams` immutable multi-dict mirroring the `Headers`
      shape. All `urllib.parse` interop lives here.
- [x] **`http.common.etag.ETag`** — frozen dataclass with `weak: bool` flag,
      `parse` / `__str__`, and `matches_strong` / `matches_weak`
      comparators.
- [x] **`http.common.http_range.HttpRange`** — frozen dataclass for byte-range
      headers (`Range`, `Content-Range`); plus `HttpRange.suffix(N)` for
      the `bytes=-N` last-N form.
- [x] **`http.common.request_conditions.RequestConditions`** — bundle of
      If-Match / If-None-Match / If-Modified-Since / If-Unmodified-Since,
      with an `apply_to(request) -> Request` helper that fans out into
      headers (wildcard `*` ETag is emitted unquoted per RFC 7232 §3.1).

### M1 leftovers / follow-ups

- [ ] **Google-style docstrings on older modules** — the M1 surface
      (bodies, Url, ETag, HttpRange, RequestConditions, HttpHeaderName) is
      converted; the legacy scaffolding (Headers, MediaType, Protocol,
      Request, Response, contexts, instrumentation, pipeline, serde) still
      carries Sphinx-style `:class:` / `:meth:` references. Convert in M2
      while touching those files anyway.
- [ ] **GitHub Actions CI** — once a remote exists.
- [ ] **Doctest examples** on the body factories and `Url.parse` —
      styleguide §14.5.

**Definition of done**: 145 tests pass (`pytest`); `mypy --strict` clean;
`ruff check` clean. ✅

---

## M2 — Composition, errors, retry, serde

The `PipelineStep` Protocol exists but there is no engine yet that runs a
chain. This milestone adds the pipeline engine, the error hierarchy that
retry depends on, the retry policy itself, a default JSON `Serde`, and the
two stateless utility policies (logger, URL redactor).

### Errors — *do this first; retry depends on it*

- [ ] **`errors/` package** modelled on azure:exceptions.py but slimmed to
      what we'll actually raise:
  - `SdkError` — top of the hierarchy. Captures `sys.exc_info()` into
    `exc_type` / `exc_value` / `exc_traceback`; carries
    `inner_exception: Optional[BaseException]` and an optional
    `continuation_token: Optional[str]`.
  - `ServiceRequestError(SdkError)` — connection refused, DNS failure,
    request never reached the server. Safe to retry on idempotent methods.
  - `ServiceResponseError(SdkError)` — request was sent but the response
    couldn't be parsed (connection drop mid-response, decode failure on
    chunked stream). Retry semantics differ from ServiceRequestError.
  - `ServiceRequestTimeoutError(ServiceRequestError)`,
    `ServiceResponseTimeoutError(ServiceResponseError)`.
  - `HttpResponseError(SdkError)` — 4xx/5xx response. Carries `status: Status`,
    `reason: Optional[str]`, `response: Response`.
  - `ClientAuthenticationError(HttpResponseError)` — 401/403; bearer-token
    policy short-circuits here without going through retry.
  - `ResourceNotFoundError(HttpResponseError)`,
    `ResourceModifiedError(HttpResponseError)`,
    `ResourceNotModifiedError(HttpResponseError)`,
    `ResourceExistsError(HttpResponseError)` — convenience subclasses for
    consumer libraries to catch.
  - `DecodeError(HttpResponseError)`, `SerializationError(ValueError)`,
    `DeserializationError(ValueError)`.
  - `StreamConsumedError(SdkError)`, `StreamClosedError(SdkError)`,
    `ResponseNotReadError(SdkError)` — body lifecycle violations.
  - `PipelineAbortedError(SdkError)` — raised when a step returns `None` (or
    raises `Abort()`) to short-circuit the chain.
  - **`ErrorMap[K, V]`** + `map_error(status_code, response, error_map)` helper
    — direct lift from azure:exceptions.py. Lets consumer libraries declare
    `{409: AlreadyExists, 412: PreconditionFailed}` once.

### Pipeline engine

- [ ] **`pipeline/policy.py`** — `Policy` ABC (sync) with:
  - `next: Policy` — set at pipeline construction
  - `send(request: Request, ctx: ExchangeContext) -> Response` — abstract
  - This is azure:_base.py:HTTPPolicy, but `.send` takes the bare Request /
    ExchangeContext pair instead of inventing a `PipelineRequest` wrapper
    (our `ExchangeContext` already carries everything `PipelineRequest`
    does, plus the trace id and span).
- [ ] **`pipeline/_sansio_runner.py`** — internal `Policy` adapter that wraps a
      SansIO `PipelineStep[Request, Request]` (for pre-send transforms) and/or
      `PipelineStep[Response, Response]` (for post-receive transforms). Steps
      that return `None` raise `PipelineAbortedError`.
- [ ] **`pipeline/_transport_runner.py`** — internal terminal `Policy` that
      wraps an `HttpClient` and turns its `execute(request)` call into the
      end of the chain. Owns the transport's context-manager lifetime when
      the pipeline does.
- [ ] **`pipeline/pipeline.py:Pipeline`** — context-manager class composing:
  - `transport: HttpClient`
  - `policies: Sequence[Policy | PipelineStep]` — in declaration order;
    SansIO steps are wrapped in `_SansIORunner` automatically.
  - Builds the linked list `policies[0] → policies[1] → … → _TransportRunner`.
  - `run(request, **kwargs) -> Response` constructs the `ExchangeContext`
    (registering it in `ContextStore`), kicks the chain, and returns the
    `Response`. `kwargs` populate `ctx.options`.
  - Context-manager `__enter__` / `__exit__` opens / closes the transport.
- [ ] **Update `ExchangeContext`** — add a `data: Dict[str, Any]` field
      (mutable, per-exchange) and an `options: Dict[str, Any]` field
      (caller-provided per-call overrides — retry counts, timeouts, …). The
      promotion chain copies `options` from the parent but starts a fresh
      `data` dict at the exchange tier.

### Retry policy

- [ ] **`pipeline/policies/retry.py:RetryPolicy(Policy)`** — modelled directly
      on azure:_retry.py with the noise stripped:
  - Configuration knobs (all kwargs to `__init__`):
    `total_retries=10`, `connect_retries=3`, `read_retries=3`,
    `status_retries=3`, `backoff_factor=0.8`, `backoff_max=120.0`,
    `retry_mode: RetryMode = Exponential`, `timeout: float = 604800`,
    `retry_on_status_codes: Iterable[int] = (408, 429, 500, 502, 503, 504)`,
    `method_whitelist: frozenset[str] = {GET, HEAD, PUT, DELETE, OPTIONS, TRACE}`,
    `respect_retry_after: bool = True`.
  - `configure_retries(options)` → fresh `settings` dict
    (`total`, `connect`, `read`, `status`, `backoff`, `max_backoff`,
    `methods`, `timeout`, `history: List[RequestHistory]`).
  - `is_retry(settings, response)`,
    `is_method_retryable(settings, request, response)` — POST/PATCH are
    retried only on 500/503/504.
  - `is_exhausted(settings)`, `increment(settings, response/error)`.
  - `get_backoff_time(settings)` — fixed or exponential.
  - `parse_retry_after(header_value)` — supports both delay-seconds and
    HTTP-date forms.
  - `_configure_positions(request, settings)` — captures `tell()` on the
    body's underlying source if any, for replay-after-retry.
  - `_configure_timeout(request, absolute_timeout, is_response_error)` —
    deducts elapsed time from the absolute budget; raises
    `ServiceRequestTimeoutError` / `ServiceResponseTimeoutError` when it
    crosses zero.
  - `send()` — the main loop: try; if `is_retry` → `increment` → `sleep`
    (via `transport.sleep`, via `Retry-After` if present and respected,
    else backoff); on `ServiceRequestError` / `ServiceResponseError` →
    classify, increment, sleep; bail on `ClientAuthenticationError`. Stores
    retry history in `ctx.data["history"]` on completion.
  - **`RequestHistory`** dataclass (in `pipeline/policies/_history.py`) —
    `(request, response, error)` snapshot per attempt, used both for
    debugging and for the `continuation_token` semantics on errors.
- [ ] Mark the existing `RetryableStep` Protocol in
      `pipeline/step/pipeline_step.py` as deprecated and drop the
      shape-specialised export in `pipeline/__init__.py`. Replace existing
      `RetryConfig` consumers with `RetryPolicy(**kwargs)` directly.

### Serde

- [ ] **`serde/json_serde.py`** — concrete implementation of the existing
      `Serde` / `Serializer` / `Deserializer` Protocols backed by stdlib
      `json`. Ships an `int`-key option, ISO-8601 datetime encoder, and
      pluggable `default` / `object_hook`. Module-level `JSON_SERDE`
      singleton for the common case. Raises `SerializationError` /
      `DeserializationError` (defined in M2 errors above).

### Instrumentation helpers

- [ ] **`instrumentation/client_logger.py:ClientLogger`** — thin facade over
      stdlib `logging` that emits structured key=value pairs and respects the
      SDK's `LogLevel`. One logger per consuming module; trace-id is pulled
      from the active `CallContext` and added as a structured field.
- [ ] **`instrumentation/url_redactor.py:UrlRedactor`** — strips userinfo and
      configurable query-param values from a URL string for safe log emission.
      Default allow-list mirrors `java-sdk`'s. Returns a `str`, not a `Url`,
      so callers can format directly.

**Definition of done**: a `Pipeline` composed of a logging SansIO step, a
`RetryPolicy`, and a stub `HttpClient` completes a happy-path request in a
test; retries fire on configured `ServiceRequestError` and on 503; auth-fail
short-circuits; tests cover the error hierarchy.

---

## M3 — Reference transport & async stack

The contracts are transport-agnostic — until something implements `HttpClient`,
nothing actually talks HTTP. This milestone ships a no-deps sync transport, the
full async hierarchy, and an async transport so the public API is usable from
both worlds.

- [ ] **`HttpClient.sleep(duration)` method** — added to the Protocol in
      `client/http_client.py` with a default `time.sleep` implementation in a
      base helper. Async twin uses `asyncio.sleep`.
- [ ] **`UrllibHttpClient`** in `client/urllib_http_client.py` — synchronous
      reference transport over `urllib.request`. Honours `RequestBody.write_to`
      (via a custom `Request.data` handler that pipes through a `BufferedSink`),
      surfaces response status / headers / body as a `BufferedSource`. Not for
      production traffic — it's the test / example transport. Implements
      `__enter__`/`__exit__` for pipeline ownership; `sleep` delegates to
      `time.sleep`.
- [ ] **`AsyncRequestBody` / `AsyncResponseBody`** at
      `http/request/async_request_body.py` / `http/response/async_response_body.py`
      — ABCs with `async def iter_bytes(chunk_size)` and either
      `async def bytes()` / `async def string()` (response) or `async def
      write_to(stream: SupportsWrite[bytes])` (request). Async factories
      from `bytes` / `AsyncIterable[bytes]` / a `BinaryIO`-like async file
      handle.
- [ ] **`AsyncHttpClient`** Protocol in `client/async_http_client.py` —
      `async def execute(request) -> AsyncResponse`; `async def
      sleep(duration)`; `async def __aenter__/__aexit__`.
- [ ] **`AsyncPolicy`** ABC (mirrors `Policy` from M2) with
      `async def send(request, ctx) -> Response` and an `async def`-aware
      `_SansIORunner` that handles both sync and async SansIO steps
      (per azure:_base.py — SansIO `on_request`/`on_response` may return
      either `None` or `Awaitable[None]`, dispatched via
      `inspect.iscoroutinefunction`).
- [ ] **`AsyncPipeline`** — async twin of `Pipeline`.
- [ ] **`AsyncRetryPolicy`** in `pipeline/policies/retry.py` — shares
      `RetryPolicyBase` with the sync version; `send` is async and uses
      `await transport.sleep(...)`.
- [ ] **`AsyncioHttpClient`** in `client/asyncio_http_client.py` — async
      reference transport built on `asyncio.open_connection` (raw sockets, no
      third-party deps). Production-quality async transports should still
      come from adapters — this is the reference.

**Definition of done**: sync and async test transports both round-trip a
request against a `socketserver`-based fixture; async test suite runs cleanly
under `pytest-asyncio`; SansIO policies run unchanged in both pipelines.

---

## M4 — Auth & advanced HTTP

- [ ] **`http/auth/access_token.py:AccessTokenInfo`** — frozen dataclass
      mirroring azure:credentials.py: `token: str`, `expires_on: int`
      (Unix seconds), `token_type: str = "Bearer"`,
      `refresh_on: Optional[int] = None`. Plus `is_expired(now=None)` and
      `needs_refresh(now=None, leeway=300)` helpers.
- [ ] **`http/auth/credentials.py`** — credential Protocols and concrete
      simple credentials:
  - `TokenCredential` Protocol — `get_token_info(*scopes, options=None) ->
    AccessTokenInfo`; `close() -> None`. Sync `ContextManager`.
  - `AsyncTokenCredential` Protocol — `async def get_token_info`,
    `async def close`. `AsyncContextManager`.
  - `KeyCredential(value: str)` — frozen, redacts under `repr`. Method
    `update(new_value)` for rotation without rebuilding the client (azure
    pattern).
  - `NamedKeyCredential(name: str, key: str)` — same, but carries both
    parts (HMAC-style auth).
  - `BasicAuthCredential(username: str, password: str)` — base64 encoded
    once at construction; `repr` redacts.
  - `TokenRequestOptions` TypedDict — optional per-call overrides (claims,
    tenant_id, etc.) forwarded to `get_token_info`.
- [ ] **`http/auth/policies.py`**:
  - `KeyCredentialPolicy(credential, header_name, *, prefix=None)` — SansIO,
    stamps a configurable header from a `KeyCredential`.
  - `BasicAuthPolicy(credential)` — SansIO, stamps `Authorization: Basic
    <base64>`.
  - `BearerTokenPolicy(credential, *scopes)` — full `Policy` (needs `.next`
    for 401-challenge handling). Caches the `AccessTokenInfo`, refreshes
    when `needs_refresh` returns True or after a 401, enforces HTTPS, and
    exposes an `on_challenge(request, response) -> bool` hook for
    consumers to handle `WWW-Authenticate` challenges (CAE, claims
    challenges, etc.) — default returns `False`.
  - `AsyncBearerTokenPolicy` — async twin.
- [ ] **Token cache** in `http/auth/token_cache.py` — pluggable cache
      (`InMemoryTokenCache` default, swap-out for Redis / file-backed) keyed
      by `(tuple(sorted(scopes)), audience)`. `BearerTokenPolicy` accepts a
      cache for sharing tokens across credentials.
- [ ] **`http/common/pagination.py`** — direct port of azure:paging.py:
  - `Pager[T]` (sync iterator-of-pages — Azure's `PageIterator`),
    `ItemPaged[T]` (flattens, with `.by_page()`).
  - `AsyncPager[T]`, `AsyncItemPaged[T]`.
  - Take a `get_next: Callable[[Optional[str]], Response]` and an
    `extract_data: Callable[[Response], Tuple[Optional[str], Iterable[T]]]`.
  - On `SdkError`, populate the error's `continuation_token` from the
    iterator's current token before re-raising.

---

## M5 — Streaming & ergonomics

- [ ] **`http.sse.SseEvent`** frozen dataclass + `SseParser` driven from an
      `Iterator[bytes]` (sync) or `AsyncIterator[bytes]` (async). Handles
      `data:` / `event:` / `id:` / `retry:` fields and multi-line `data`
      reassembly per the WHATWG SSE spec. (corehttp doesn't ship this — we
      own it.)
- [ ] **Streaming JSON deserialization** helper — consumes a
      `ResponseBody.iter_bytes()` into the default JSON `Serde` line by
      line. Useful for newline-delimited JSON (`application/jsonl`) and SSE
      payloads.
- [ ] **Multipart form-data builder** — `RequestBody.from_multipart(parts)`
      factory producing a replayable `multipart/form-data` body with a
      generated boundary.
- [ ] **Chunked transfer-encoding writer** — utility that frames an
      `Iterator[bytes]` into HTTP/1.1 chunk framing for transports that need
      it manually.

---

## M6 — Observability integrations

These are *optional* extras; the core stays no-deps.

- [ ] **`dexpace-sdk-otel`** sibling package — implements `Tracer` / `Span` /
      `TracingScope` over OpenTelemetry's Python API. Provides an
      `OpenTelemetryTracer.install()` shortcut. Span attributes follow the
      OTel semantic conventions used by corehttp:
      `http.request.method`, `http.response.status_code`, `url.full`,
      `server.address`, `server.port`, `http.request.resend_count`,
      `error.type`.
- [ ] **Metrics interface** in `instrumentation/metrics/` — `Counter` /
      `Histogram` / `Gauge` ABCs plus a `MetricsContext` factory analogous to
      `InstrumentationContext`. Ship noop singletons; real implementations in
      `dexpace-sdk-otel`.
- [ ] **Built-in `LoggingPolicy`** (SansIO) — emits a structured log line per
      request via `ClientLogger` + `UrlRedactor`. One sansio step,
      `on_request` logs outbound, `on_response` logs status + duration.
- [ ] **Built-in `TracingPolicy`** (full `Policy`, needs `.next`) — opens a
      span around the downstream chain via the installed `Tracer`. Records
      retry count from `ctx.data["history"]`. Per-call opt-out via
      `ctx.options["tracing_enabled"] = False`.

---

## M7 — Documentation

- [ ] `docs/architecture.md` — high-level design, package map, data flow.
- [ ] `docs/bodies.md` — `RequestBody` / `ResponseBody` factories, single-use
      vs replayable, `iter_bytes` chunking, file/stream/iter shapes, the
      logging decorators.
- [ ] `docs/http.md` — request/response models, headers, media types, context
      promotion chain, `HttpClient` Protocol.
- [ ] `docs/pipelines.md` — pipeline composition, SansIO step vs Policy,
      retry semantics, the step Protocols, common built-in steps.
- [ ] `docs/body-logging.md` — `LoggableRequestBody` / `LoggableResponseBody`
      mechanics, snapshot caps, concurrency model.
- [ ] `docs/auth.md` — credential types, token caching, integration with
      pipelines.
- [ ] `docs/errors.md` — error hierarchy, when each is raised, ErrorMap.
- [ ] Sphinx-generated API reference under `docs/api/` (autodoc + intersphinx
      to stdlib). Optional; only worth it once the surface stabilises.

---

## Cross-cutting items (do alongside any milestone)

- [ ] **`__all__` audits** — every `__init__.py` and every public module
      already declares `__all__`; keep it accurate as new symbols land.
- [ ] **Versioning policy** — pre-1.0 follows zerover: `0.MINOR.PATCH`, breaking
      changes only on minor bumps. Document in `CONTRIBUTING.md` once the
      project takes external contributors.

---

## Known carry-overs from the initial scaffolding

Items that landed in the first commits but want a second pass.

- [x] **`Headers.__repr__`** — now emits a dict-style summary.
- [x] **`io/` module rip-out** — replaced by Pythonic `RequestBody` /
      `ResponseBody` abstractions; the Java/Okio-style seam was over-engineered
      for Python.
- [ ] **`Status.from_code`** — Java's SDK exposes a `fromCodeOrNull` /
      `fromCode` pair. Python `IntEnum(code)` already does the first;
      consider an explicit classmethod for symmetry with the Java SDK so
      docs and examples stay parallel.
- [ ] **`pipeline/step/pipeline_step.py:RetryableStep`** — replaced by M2's
      `Policy` ABC; remove once M2 lands and downstream consumers (if any)
      have migrated.
- [ ] **Google-style docstring sweep across legacy files** — see M1
      leftovers above.

---

## How to pick this up in a new session

1. Read this file end-to-end, then `CLAUDE.md`, then `README.md`.
2. Pick a milestone — start at the top of M1 unless told otherwise.
3. Skim the Azure SDK counterpart for the item you're working on if one
   exists — `azure-sdk-for-python/sdk/core/corehttp/corehttp/` is checked in
   at the repo root for offline reference. Use it for shape, not for syntax.
   Python idioms specific to *our* SDK (frozen `slots=True` dataclasses over
   builders, Protocols over interfaces+adapters, context managers over
   `Closeable`+`close`, `from __future__ import annotations` everywhere,
   modern 3.12 typing, Google-style docstrings) take precedence.
4. Java-SDK shapes (`dexpace/java-sdk`) cover symbols Azure omits; Azure
   covers the runtime mechanics (retry, auth, paging, errors) Java's SDK
   builders model less directly.
5. Land tests in the same PR as the code; CI must stay green.
6. Update this file as items move from `[ ]` to `[x]` and add follow-ups you
   discover at the bottom of the relevant milestone.
