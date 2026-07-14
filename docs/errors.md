# Error model

The SDK's exception hierarchy is rooted at `SdkError`. The shape was
chosen so callers can either catch broadly (`except SdkError`) or
distinguish failure modes by subclass.

```
Exception
└─ SdkError                              # root; captures sys.exc_info + inner_exception
   ├─ NetworkError  (also OSError)       # transport branch; never got a response;
   │  │                                  #   always retryable at the error-class level
   │  └─ ServiceRequestError             # request never reached server
   │     └─ ServiceRequestTimeoutError   # connect-phase timeout (retryable)
   ├─ RequestCancelledError  (also OSError)  # sync cooperative-cancel; terminal, never retried
   ├─ ServiceResponseError               # response unparseable / connection dropped
   │  └─ ServiceResponseTimeoutError     #   (not the always-retryable transport branch)
   ├─ HttpResponseError                  # 4xx/5xx received intact; carries bounded error body
   │  ├─ ClientAuthenticationError       # 401/403 — short-circuits retry
   │  ├─ DecodeError                     # body could not be decoded
   │  ├─ ResourceExistsError             # typically 409
   │  ├─ ResourceNotFoundError           # typically 404
   │  ├─ ResourceModifiedError           # typically 412 (precondition failed)
   │  └─ ResourceNotModifiedError        # 304
   ├─ StreamConsumedError                # body already consumed
   ├─ StreamClosedError                  # body closed before reading
   ├─ ResponseNotReadError               # attribute access before read
   ├─ StreamingError                     # stream framing / decode error (e.g. SSE)
   ├─ PipelineAbortedError               # SansIO step returned None
   └─ SerdeError                         # serde failure root; also a ValueError
      ├─ SerializationError              # encode side
      └─ DeserializationError            # decode side (CodecError lives here)
```

The taxonomy has two branches, split by whether a response was ever received:

- **`NetworkError`** roots the transport branch — a request that never yielded a
  usable response (connection refused, DNS failure, connect timeout). It also
  subclasses `OSError`, so `except OSError:` sites (in user code or third-party
  libraries) keep matching transport failures after the SDK re-wraps them, and
  it is *always retryable* at the error-class level (a request that never
  reached the service is safe to re-send on an idempotent method). Never catch a
  `NetworkError` and re-wrap it into a non-network branch — that would break
  those `except OSError:` sites.
- **`HttpResponseError`** roots the response branch — a 4xx/5xx response arrived
  intact. Its `retryable` flag is derived per-status from the shared retry
  classifier and *baked once* at construction, so a later change to a retry
  policy's configured status set never mutates an already-built error. It also
  carries a bounded, replayable copy of the error body via
  `read_body(max_bytes=ERROR_BODY_MAX_BYTES)` (default cap: 1 MiB) for repeatable
  post-mortem inspection; `body_snapshot` remains the non-consuming preview.

`ServiceResponseError` is the in-between case (request sent, but the response
could not be parsed): it is deliberately *not* on the always-retryable transport
branch, because the request may already have been processed.

`RequestCancelledError` is the terminal type for synchronous cooperative
cancellation. Like `NetworkError` it is an `OSError`, but — being terminal — it
is not a `NetworkError` and is never retryable. Discriminate a timeout from a
cancellation by subtype (a `ServiceRequestTimeoutError` is a retryable
`NetworkError`; a `RequestCancelledError` is not), checking the timeout first,
never by matching message text. The asynchronous cancel signal is
`asyncio.CancelledError`, a `BaseException` that needs no SDK subtype.

On the sync path, cancellation and deadlines are honoured at attempt boundaries
and during backoff waits — the retry loop's `CancellationToken` sleep returns
early the moment another thread cancels. An already-issued blocking socket read
is *not* interrupted mid-read, though: CPython threads cannot be pre-empted from
outside, so an in-flight read runs to its own transport read timeout before the
retry loop regains control. That is a property of the threading model, not a
missing feature; callers needing tighter mid-read cancellation set an aggressive
transport read timeout or use the async stack. See [`deviations.md`](deviations.md).

`SerdeError` roots the serde failure taxonomy: catch it to handle any
serde failure regardless of direction, or catch `SerializationError` /
`DeserializationError` to distinguish encode from decode. All three are
also `ValueError`s, so existing `except ValueError` handlers keep working.

`SdkError` captures `sys.exc_info()` at construction time, preserving
the original cause even when the SDK re-wraps a stdlib exception.

## Mapping status codes to exceptions

Consumer libraries declare a per-operation table and use `map_error`
after each send:

```python
from dexpace.sdk.core.client import HttpClient
from dexpace.sdk.core.errors import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    map_error,
)
from dexpace.sdk.core.http.request import Request
from dexpace.sdk.core.http.response import Response

ERROR_MAP = {
    404: ResourceNotFoundError,
    409: ResourceExistsError,
    412: ResourceModifiedError,
}


def send(client: HttpClient, request: Request) -> Response:
    with client.execute(request) as response:
        if not response.is_success:
            map_error(int(response.status), response, ERROR_MAP)
        return response
```

`map_error` only raises when `status_code` is a key in the supplied
map; an unmapped code (or a `None` map) is a no-op and returns without
raising. There is no default error type for unmatched codes — handle
the long tail yourself after `map_error` returns.

## Continuation tokens

`SdkError.continuation_token` is auto-populated by the paging
iterators (`Pager` / `AsyncPager` / `ItemPaged` / `AsyncItemPaged`)
when a `get_next` call raises. Callers can resume by passing the token
to `by_page(continuation_token=…)`.
