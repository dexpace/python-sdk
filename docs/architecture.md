# Architecture

The `dexpace-sdk-core` package is an **HTTP-client toolkit, not an HTTP
client**. It ships abstractions, models, and pipelines; consuming libraries
plug in a concrete transport via the `HttpClient` Protocol.

## Layers

```
┌─────────────────────────────────────────────────────────────────┐
│              Consumer (your client library)                     │
│   Constructs Pipeline, picks policies, owns request shapes      │
└───────────────────┬──────────────────────────────┬──────────────┘
                    │                              │
         ┌──────────▼────────┐          ┌──────────▼──────────┐
         │   pipeline/       │          │   http/auth/        │
         │   - Pipeline      │          │   - BearerToken     │
         │   - Policy        │          │   - KeyCredential   │
         │   - PipelineStep  │          │   - BasicAuth       │
         │   - redirect,     │          │   - TokenCache      │
         │     idempotency,  │          │                     │
         │     retry,        │          │                     │
         │     set-date,     │          │                     │
         │     client-       │          │                     │
         │     identity,     │          │                     │
         │     logging,      │          │                     │
         │     tracing       │          │                     │
         └──────────┬────────┘          └─────────────────────┘
                    │
         ┌──────────▼─────────────────────────────────────────┐
         │   http/  request/response/common/context/sse       │
         │   - frozen-dataclass Request / Response            │
         │   - Headers, MediaType, Url, ETag, HttpRange       │
         │   - RequestBody / ResponseBody (sync + async)      │
         │   - SSE parser, multipart, JSONL, chunked frame    │
         │   - DispatchContext → RequestContext → Exchange    │
         └──────────┬─────────────────────────────────────────┘
                    │
         ┌──────────▼─────────────────────────────────────────┐
         │   client/  HttpClient + AsyncHttpClient            │
         │   - Protocols only; transports plug in here        │
         └──────────┬─────────────────────────────────────────┘
                    │
         ┌──────────▼─────────────────────────────────────────┐
         │   dexpace-sdk-http-stdlib (separate distribution)  │
         │   - UrllibHttpClient (sync reference)              │
         │   - AsyncioHttpClient (async reference)            │
         └────────────────────────────────────────────────────┘
```

## Data flow (sync)

1. Consumer builds a `Request` (immutable, frozen dataclass).
2. Consumer builds a `DispatchContext` carrying the `InstrumentationContext`.
3. `Pipeline.run(request, dispatch, **options)`:
   - Promotes `DispatchContext` → `RequestContext`, registers in
     `ContextStore`.
   - Wraps in a mutable `PipelineContext` (`options` + `data` dicts).
   - Walks the policy chain in order; each policy may mutate the
     request, call `self.next.send(...)`, and post-process the response.
   - Terminal `_TransportRunner` calls `HttpClient.execute(...)`.
   - On return, the immutable context is promoted again to an
     `ExchangeContext` (registered in `ContextStore`).
4. The `Response` (with a single-use `ResponseBody`) is returned to the
   caller. `with response:` releases the transport handle.

## Async variant

Same layering, with `AsyncPipeline` / `AsyncPolicy` / `AsyncHttpClient` /
`AsyncResponse` / `AsyncResponseBody`. SansIO policies (callable
`(value, ctx) -> value`) work in both sync and async pipelines — the
async pipeline auto-awaits any coroutine return.

## Why no `IoProvider`

The Java port has an `IoProvider` / `Buffer` / `Source` / `Sink` layer
(a port of Okio). In Python, `bytes` / `bytearray` / `memoryview` /
`BytesIO` / `BinaryIO` already cover the same surface idiomatically.
Bodies use `iter_bytes(chunk_size)` for streaming and ordinary stdlib
primitives for everything else. See the "Things That Will Bite You"
section in `CLAUDE.md` for the design rationale.
