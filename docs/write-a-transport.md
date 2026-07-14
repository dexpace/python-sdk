# Writing a transport

A transport is the one piece of the toolkit the SDK does not ship: `core`
provides models, pipelines, retry, auth, and logging, but it never opens a
socket. You supply that by implementing the `HttpClient` Protocol (or its async
twin `AsyncHttpClient`) over whatever HTTP library you already trust, then
certify the adapter against the shared conformance kit.

Four transports ship this way already — `urllib`/`asyncio` (the
zero-dependency reference), `httpx`, `aiohttp`, and `requests`. This guide is
how you add a fifth.

## The seam

```python
from __future__ import annotations

from dexpace.sdk.core.client import HttpClient
from dexpace.sdk.core.http.request import Request
from dexpace.sdk.core.http.response import Response


class MyHttpClient(HttpClient):
    """Adapt one HTTP library to the transport seam."""

    def execute(self, request: Request) -> Response:
        # 1. translate the immutable Request into the native library's shape,
        # 2. send it, and
        # 3. adapt the native response back into an SDK Response.
        raise NotImplementedError
```

`HttpClient` is a single method: send one `Request`, return one `Response`.
The async twin is the same shape with `async def execute` returning an
`AsyncResponse`. Both are `runtime_checkable` Protocols, so you implement them
structurally — you do not have to inherit from anything, and subclassing is
shown above only for readability.

The transport is deliberately dumb. It does **not** follow redirects, retry,
authenticate, log, or trace: those are pipeline policies that wrap the transport
from the outside. A transport that quietly retries or follows a `Location`
header will double-count against the retry budget and defeat the redirect
security battery, so the contract forbids it.

## What a correct transport must do

The requirement IDs below are the project's own spec vocabulary; each resolves
to a marked test in the suite. The rest of the contract is enforced by the
conformance battery described under "Certifying your adapter".

### Response shape and inertia

- Return a `Response` (never `None`) for any exchange that produced a status
  line. The body is **not** pre-buffered — hand back a streaming
  `ResponseBody` and let the caller drain and close it.
- Do not follow redirects. A `301`/`302`/`303`/`307`/`308` is returned as-is.
- Do not retry. One `execute` call is one native exchange.

### Error mapping into the SDK taxonomy

Translate native failures into the `errors` hierarchy so pipeline policies can
classify them uniformly (see [`errors.md`](errors.md)):

| Native condition | SDK exception | Retryable? |
|------------------|---------------|------------|
| Connection refused / DNS failure | `ServiceRequestError` (a `NetworkError`) | yes, always |
| Connect-phase timeout | `ServiceRequestTimeoutError` | yes |
| Read-phase timeout | `ServiceResponseTimeoutError` | via response branch |
| Cooperative cancellation | `RequestCancelledError` | no — terminal |
| Other mid-exchange transport error | `ServiceRequestError` | yes |

`NetworkError` also subclasses `OSError`, so an `except OSError:` site in caller
or library code keeps matching transport failures after you re-wrap them — never
re-wrap a `NetworkError` onto a non-network branch. A timeout and a cancellation
are both `OSError` subtypes, so a classifier must test the timeout type
**before** the cancellation type or it will misread one as the other.

### Content-Type and adapter-computed framing

- An explicit `Content-Type` on the request is authoritative; never overwrite
  it. When the request omits one, derive it from the body's media type.
- Compute `Content-Length` yourself from the buffered body. A body whose length
  is unknown up front frames as `Transfer-Encoding: chunked` with **no**
  `Content-Length`. A transport that always buffers before sending knows the
  exact length once buffering completes and uses `Content-Length` even then — it
  must not fake chunked framing.
- An empty-body-bearing method (`POST` with an empty body) sends a zero-length
  body; a bodyless `GET` sends no `Content-Length` at all.

### Header hygiene

- `Headers` compares names case-insensitively and stores them lower-cased
  (**HEADER-04**); honour that on both directions.
- A header the model layer accepts but the native library rejects is dropped,
  **counted, and logged** — never silently discarded. The drop log is bounded
  by distinct name so a hostile stream of unique bad names cannot grow it
  without limit.
- A malformed inbound header is dropped while obs-text (opaque byte) values are
  preserved; a malformed inbound `Content-Length` or `Content-Type` is
  downgraded rather than allowed to abort the whole response.

### Request body semantics

- A single-use body (`from_stream` / `from_iter`) is written to the wire
  **exactly once** (**BODY-08** — a second consumption raises `RuntimeError`).
- A replayable body (`from_bytes` / `from_string` / `from_form` / `from_file`)
  replays byte-identical across attempts (**BODY-21**). The retry policy calls
  `to_replayable()` before the first send when a retry budget is positive, so
  the transport itself only has to preserve whatever body it is handed — see
  [`bodies.md`](bodies.md).
- A failure while buffering a body for replay surfaces cleanly, without leaking a
  half-written native request.

### Response lifecycle

- If SDK-level adaptation of a native response fails, close the native response
  before propagating.
- Closing a streamed `Response` cascades the close to the native handle; an
  abandoned or partially-read stream still releases the underlying producer.

### Timeouts, concurrency, status totality, and close

- A sub-resolution timeout clamps up to the adapter's smallest granularity
  rather than truncating to zero. Per-call timeouts do not bleed between
  concurrent calls, and `execute` is safe to call from multiple threads
  (or, for the async twin, concurrently on one event loop).
- `Status` is total over integers (**TRANSPORT-24**): any status code the peer
  sends — in range, out of range, or vendor-specific — surfaces intact with its
  body. No valid status ever fails adaptation.
- Close is ownership-aware: an adapter built over a caller-supplied (BYO) native
  client must leave that client open when the adapter closes, and calling
  `execute` after close raises.
- Proxy credentials never leak into logs or errors, and any proxy feature the
  adapter cannot honour is discoverable rather than silently ignored.

## Certifying your adapter

The contract above is executable. It lives in the `dexpace-sdk-tck` package as a
transport-neutral battery that drives a *harness* — a small seam between the
test bodies and your concrete adapter. You certify an adapter by implementing
one harness and registering it; no test body changes.

1. Implement `SyncTransportHarness` (and/or `AsyncTransportHarness`) from
   `dexpace.sdk.tck.harness`. The harness programs the next native exchange
   (`expect_response`, `expect_connect_timeout`, `expect_cancellation`, …) and
   exposes what the adapter did (`sent_requests`, `attempt_count`, `drop_log`,
   `native_responses`).

2. Lower any capability flag your transport genuinely cannot simulate
   (`supports_streaming`, `supports_cancellation`,
   `supports_adaptation_failure`, …). The battery **skips** the facets a flag
   turns off rather than failing them, so a minimal transport is not forced to
   fake a native quirk it does not have.

3. Register the harness from your package's own `tests/conftest.py`:

   ```python
   from dexpace.sdk.tck.harness import SYNC_HARNESS_FACTORIES


   def register() -> None:
       from my_harness import MyTransportSyncHarness

       SYNC_HARNESS_FACTORIES.append(("my-transport", MyTransportSyncHarness))
   ```

Then run the battery against every registered adapter:

```bash
pytest --pyargs dexpace.sdk.tck
```

A green run certifies your transport against the full contract above — success
shape, error taxonomy, framing, header hygiene, body semantics, response
lifecycle, timeouts, status totality, ownership-aware close, and proxy safety.
