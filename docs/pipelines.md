# Pipelines

The pipeline is the orchestration layer that drives a request through
an ordered sequence of policies and a terminal transport. It exists in
two parallel variants:

| Sync                              | Async                                  |
|-----------------------------------|----------------------------------------|
| `Pipeline`                        | `AsyncPipeline`                        |
| `Policy` (`.next`, `send`)        | `AsyncPolicy` (`.next`, `async send`)  |
| `PipelineStep` Protocol           | Same Protocol; pipeline auto-awaits coroutine returns |
| `HttpClient` transport            | `AsyncHttpClient` transport            |
| `Response`                        | `AsyncResponse`                        |

## Two kinds of pipeline entries

1. **SansIO step** — a plain callable
   `(value, ctx) -> value | None`. Used for stateless transforms:
   header stamping, redaction, payload sanitisation. The pipeline
   wraps these in an internal SansIO runner. Return `None` to abort
   the chain (raises `PipelineAbortedError`).

   Tag with a `side` attribute to opt into response-side wrapping:

   ```python
   def stamp_response(response, ctx):
       return response.with_header("X-Server-Stamp", "ok")
   stamp_response.side = "response"
   ```

2. **Full `Policy`** — extends `pipeline.Policy` and implements
   `send(request, ctx)`. Use this when the step needs to wrap the
   downstream chain (retry, auth-challenge handling, span lifecycles).

## Construction

```python
from dexpace.sdk.http.stdlib import UrllibHttpClient
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import (
    LoggingPolicy,
    OperationTracingPolicy,
    RetryPolicy,
    TracingPolicy,
)

with Pipeline(
    UrllibHttpClient(),
    policies=[OperationTracingPolicy(), TracingPolicy(), LoggingPolicy(), RetryPolicy()],
) as pipeline:
    response = pipeline.run(request, dispatch_ctx)
```

The chain runs in declaration order. For the example above:

1. `OperationTracingPolicy` emits `operation_started` for the whole call.
2. `TracingPolicy` opens a span.
3. `LoggingPolicy` logs the request.
4. `RetryPolicy` invokes the transport; retries on transient failures.
5. The transport runner calls `UrllibHttpClient.execute(request)`.
6. The unwinding mirrors that order: logging emits the response, tracing
   closes the span, and operation-tracing emits the single
   `operation_succeeded` / `operation_failed` once the call settles.

In the canonical `default_pipeline`, `OperationTracingPolicy` sits *outside*
the redirect and retry wrappers (so the per-operation lifecycle fires once and
reflects the final outcome), while `TracingPolicy` sits *inside* them and opens
one span per attempt / hop.

## Built-in policies

| Policy                              | Purpose                                                  |
|-------------------------------------|----------------------------------------------------------|
| `RetryPolicy` / `AsyncRetryPolicy`  | Retry transient failures with backoff + `Retry-After`. Auto-buffers single-use request bodies when `total_retries > 0`. Retryable status set is configurable (see below). |
| `RedirectPolicy` / `AsyncRedirectPolicy` | Follow `301`/`302`/`303`/`307`/`308`, stripping credentials across origins. The async twin is gated off by default (see below). |
| `LoggingPolicy` / `AsyncLoggingPolicy` | Structured request/response wire logs with URL redaction and a bounded body preview. |
| `OperationTracingPolicy` / `AsyncOperationTracingPolicy` | Emit the per-operation lifecycle (`operation_started`, then one `operation_succeeded`/`operation_failed`) from outside the retry/redirect loop. |
| `TracingPolicy` / `AsyncTracingPolicy` | Open a span per attempt and emit per-request tracer events; OTel semantic-conv attributes. |
| `BearerTokenPolicy` / `AsyncBearerTokenPolicy` (auth) | Acquire + cache + apply OAuth bearer tokens.  |
| `KeyCredentialPolicy` (auth)        | Stamp an API key into a configurable header.              |
| `BasicAuthPolicy` (auth)            | `Authorization: Basic <base64>`.                          |

Both observability policies now ship an async twin. `AsyncLoggingPolicy` and
`AsyncTracingPolicy` are wired into `default_async_pipeline` at the same stages
their sync counterparts occupy, so the async stack emits the same per-attempt
wire logs and per-attempt tracing span as the sync default — the two pillars are
at parity here.

## Mutable scratchpad — `PipelineContext`

Each policy receives a `PipelineContext` with:

- `call`: the immutable `RequestContext` (for trace correlation).
- `options`: a `dict[str, Any]` of caller-supplied per-call overrides.
  Populated from `Pipeline.run(**options)`. The retry policy pulls
  knobs like `retry_total` from here.
- `data`: a `dict[str, Any]` for policy bookkeeping (retry counters,
  challenge state, etc.).

Per-call opt-outs follow a convention:
`ctx.options["logging_enabled"] = False`,
`ctx.options["tracing_enabled"] = False`,
`ctx.options["enforce_https"] = False`.

## Retry and single-use bodies

`RequestBody.from_stream` and `RequestBody.from_iter` are single-use — the
second `iter_bytes()` call raises `RuntimeError`. To keep retries safe
without forcing every caller to remember `to_replayable()`, both
`RetryPolicy.send` and `AsyncRetryPolicy.send` inspect the body up front
and, when the *effective* retry total for the call is positive, swap in a
buffered replayable copy before the first attempt:

```python
settings = self._configure_settings(ctx.options)
if settings["total"] > 0 and request.body is not None and not request.body.is_replayable():
    request = request.with_body(request.body.to_replayable())
```

The decision reads the effective total *after* merging any per-call
`retry_total` override (from `ctx.options`) with the constructor default, so a
per-call `retry_total=3` over an instance built with `total_retries=0` still
buffers, and a per-call `retry_total=0` (or `RetryPolicy.no_retries()`) skips
the buffering so callers who opt out of retries pay no memory cost for a copy
they will never use. Already-replayable bodies (`from_bytes`, `from_string`,
`from_form`) flow through untouched because `to_replayable()` returns `self`.

## Retryable HTTP status codes

`RetryPolicy` treats a fixed set of status codes as transient and retryable:
`408`, `429`, and the transient `5xx` family (`500`, `502`, `503`, `504`) —
`DEFAULT_RETRYABLE_STATUS`. Permanent `5xx` codes (for example `501`) are not
retried. The set is configurable per policy through the
`retry_on_status_codes` constructor argument, and a per-call `retry_status`
override in `ctx.options` caps how many status-driven retries a single call
spends.

The classification is *baked once* into `HttpResponseError.retryable` at
construction time, so changing a policy's configured status set later never
mutates an already-built error — see [`errors.md`](errors.md).

## Default stacks and the redirect asymmetry

`default_pipeline(client)` and `default_async_pipeline(client)` assemble the
canonical stack. The stage order is operation-tracing → redirect → idempotency →
retry → set-date → client-identity → [auth] → logging → tracing. Each policy is
opt-out (pass `None`) or opt-in-with-override (pass a pre-configured instance).

The two stacks are at parity on observability — both wire per-attempt wire
logging and a per-attempt tracing span, bracketed by the per-operation tracer —
but they differ on **redirect**:

- `default_pipeline` wires `RedirectPolicy` by default.
- `default_async_pipeline` does **not** wire `AsyncRedirectPolicy` unless the
  caller passes one explicitly (`default_async_pipeline(client,
  redirect=AsyncRedirectPolicy(...))`).

This is not an async-only quirk. `AsyncRedirectPolicy` delegates every per-hop
decision to the same shared `RedirectPolicy` core the sync default wires
unconditionally, so the gaps are shared: an `https://` → `http://` downgrade is
followed rather than default-denied, and a cross-origin hop strips only
`Authorization` (not `Cookie` / `Proxy-Authorization`), judged against the
previous hop rather than the seed origin. Gating the async twin off — rather
than also removing redirect from the sync default, a larger behaviour change — is
the narrower fix while the shared core is certified against the full redirect
security battery. `Location` resolution itself is wire-exact. The full battery,
its `xfail`-tracked dimensions, and the rationale are in
[`deviations.md`](deviations.md).
