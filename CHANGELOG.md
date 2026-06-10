# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

A round of platform improvements to `dexpace-sdk-core`: new optional building
blocks (typed serialization, webhook verification, pagination, two pipeline
policies), tightened retry and tracing behaviour, and a batch of correctness
fixes across bodies, SSE parsing, Digest auth, and error reporting. Most of this
lands in `core`; the transport adapters additionally get consistent connect- vs
read-phase timeout classification and tighter resource release. The only removed
public symbol is the unused `RetryConfig` (see Removed); existing code otherwise
continues to work without modification.

### Added

- **Tristate values** (`serde.tristate`). A three-way type distinguishing
  "set to a value", "explicitly set to null", and "absent", so partial updates
  (PATCH-style payloads) can round-trip an explicit `null` without conflating it
  with an omitted field.
- **Typed model codec** (`serde.codec`). A small encode/decode layer over the
  existing `Serde` protocol for converting between typed models and wire bytes,
  built on the standard library only. This is the largest new surface and is
  worth a careful read before depending on it.
- **Webhook signature verification** (`http.webhooks`). Helpers to verify the
  authenticity of inbound webhook payloads using constant-time comparison.
- **Pagination** (`pagination`). A paginator abstraction with pluggable
  next-page strategies, a `Link` header parser, and a page model, so list
  endpoints can be iterated without each caller re-implementing cursor handling.
- **Idempotency-key policy** (`pipeline.policies.idempotency`, plus its async
  twin). Stamps a generated idempotency key onto retriable, non-idempotent
  requests so safe automatic retries don't double-apply a side effect.
- **Client-identity policy** (`pipeline.policies.client_identity`, plus its
  async twin). Sets a consistent `User-Agent` / client-identity header derived
  from the configured application id and SDK version.
- **HTTP tracer** (`instrumentation.http_tracer`). An adapter-style tracer base
  whose per-event methods default to no-ops, so a subclass overrides only the
  events it cares about. Wired through the tracing policy for span emission.
- **Log correlation** (`instrumentation.correlation`). A `contextvar`-backed
  correlation id that flows through the pipeline and is attached to log records,
  so logs from a single logical request can be tied together.
- **Reconnecting SSE client** (`http.sse.connection`). `SseConnection` and
  `AsyncSseConnection` resume an interrupted event stream by replaying the
  `Last-Event-ID` header and reconnecting with jittered backoff that honours the
  server's `retry:` hint. Built on the shared dispatch seam
  (`pipeline.dispatch`), which lets both the SSE client and the paginator accept
  either a pipeline or a bare send-callable.

### Changed

- **Retry tuning** (`pipeline.policies.retry` / `async_retry`). More
  configurable backoff and clearer rules for which responses and exceptions are
  retried, including respecting `Retry-After`. The async retry path now observes
  cancellation cleanly between attempts.
- **Tracing and redirect policies** now emit tracer events and carry correlation
  through redirects, with credentials stripped on cross-origin redirects.
- **Default pipelines** (`pipeline.defaults`). The standard sync/async stacks now
  assemble the new idempotency and client-identity policies alongside the
  existing retry, redirect, logging, and tracing policies.
- **Loggable bodies** (`http.request.loggable_request_body`,
  `http.response.loggable_response_body`). Capture is bounded and repeatable
  reads behave correctly; the byte cap is honoured on the tap without truncating
  the primary write path.
- **Error reporting** (`errors.http`). HTTP errors now expose whether they are
  `retryable` and carry a bounded body snapshot for diagnostics, with the
  snapshot capped so an error never holds an unbounded payload.
- **`HttpRange.suffix`** (`http.common.http_range`) now returns a public
  `HttpRange` (carrying an `is_suffix` flag) instead of a private helper type,
  so a `bytes=-N` suffix range composes with `HttpRange.format_many` alongside
  ordinary ranges.
- **`CallContext`** (`http.context`) is now an `abc.ABC`. It declares no
  abstract methods, so existing subclasses are unaffected; the change only
  prevents the base from being instantiated directly.

### Removed

- **`RetryConfig`** (`pipeline` / `pipeline.step.config`). It was exported but
  never wired into the retry policy, so it configured nothing; `RetryPolicy`'s
  constructor is the real configuration surface. Code that imported
  `RetryConfig` should configure `RetryPolicy` directly.

### Fixed

- **SSE parsing** (`http.sse.parser`) now strips a leading UTF-8 byte-order mark
  and cleans up the async stream deterministically on cancellation or exit.
- **Digest auth** (`http.auth.digest`) honours the server-advertised charset
  when computing the digest, fixing authentication against servers that send
  non-ASCII credentials.
- **MediaType** (`http.common.media_type`) handles parameter parsing edge cases
  (quoting, casing, and whitespace) more robustly.
- **Async response cancellation** (`http.response.async_response`,
  `async_response_body`). Cancelling an in-flight read now releases the
  underlying resources instead of leaking them, and re-raises `CancelledError`
  after cleanup.

### Verified

- `mypy --strict`, `ruff check`, `ruff format --check`, and `pytest` run in CI
  across the supported Python matrix (3.12–3.14). New modules ship with tests
  under each package's `tests/` tree, and `py.typed` continues to ship so
  downstream type-checkers consume the annotations.

### Honest scope boundaries

The following were intentionally left out of this round and are **not** included:

- **Default error map** — error classification beyond the `retryable`
  flag and body snapshot was deferred; callers still map status codes to domain
  errors themselves.
- **`sendfile` fast-path** — file bodies are streamed via the existing
  `iter_bytes` path; no zero-copy `sendfile` transport optimisation was added.
- **MCP support** — no Model Context Protocol integration is included.
- **Java SDK items** — the Java counterpart lives in a separate repository and
  was out of scope here.
- **Code generation** — no client/model code generation was added; all surfaces
  in this release are hand-written.

[Unreleased]: https://github.com/dexpace/python-sdk/compare/main...HEAD
