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
from dexpace.sdk.core.client import UrllibHttpClient
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import LoggingPolicy, RetryPolicy, TracingPolicy

with Pipeline(
    UrllibHttpClient(),
    policies=[TracingPolicy(), LoggingPolicy(), RetryPolicy()],
) as pipeline:
    response = pipeline.run(request, dispatch_ctx)
```

The chain runs in declaration order. For the example above:

1. `TracingPolicy` opens a span.
2. `LoggingPolicy` logs the request.
3. `RetryPolicy` invokes the transport; retries on transient failures.
4. The transport runner calls `UrllibHttpClient.execute(request)`.
5. The unwinding mirror order: logging emits the response, tracing
   closes the span.

## Built-in policies

| Policy                              | Purpose                                                  |
|-------------------------------------|----------------------------------------------------------|
| `RetryPolicy` / `AsyncRetryPolicy`  | Retry transient failures with backoff + `Retry-After`. Auto-buffers single-use request bodies when `total_retries > 0`.    |
| `LoggingPolicy`                     | Structured request/response logs with URL redaction.      |
| `TracingPolicy`                     | Open a span per request; OTel semantic-conv attributes.   |
| `BearerTokenPolicy` (auth)          | Acquire + cache + apply OAuth bearer tokens.              |
| `KeyCredentialPolicy` (auth)        | Stamp an API key into a configurable header.              |
| `BasicAuthPolicy` (auth)            | `Authorization: Basic <base64>`.                          |

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
and, when `total_retries > 0`, swap in a buffered replayable copy before
the first attempt:

```python
if total_retries > 0 and request.body is not None and not request.body.is_replayable():
    request = request.with_body(request.body.to_replayable())
```

`total_retries == 0` (e.g. `RetryPolicy.no_retries()`) skips the buffering
step so callers who explicitly opt out of retries pay no memory cost for a
copy they will never use. Already-replayable bodies (`from_bytes`,
`from_string`, `from_form`) flow through untouched because
`to_replayable()` returns `self`.
