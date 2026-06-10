# Error model

The SDK's exception hierarchy is rooted at `SdkError`. The shape was
chosen so callers can either catch broadly (`except SdkError`) or
distinguish failure modes by subclass.

```
Exception
└─ SdkError                              # root; captures sys.exc_info + inner_exception
   ├─ ServiceRequestError                # request never reached server
   │  └─ ServiceRequestTimeoutError
   ├─ ServiceResponseError               # response unparseable / connection dropped
   │  └─ ServiceResponseTimeoutError
   ├─ HttpResponseError                  # 4xx/5xx received intact
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
   ├─ SerializationError                 # also a ValueError
   └─ DeserializationError               # also a ValueError
```

`SdkError` captures `sys.exc_info()` at construction time, preserving
the original cause even when the SDK re-wraps a stdlib exception.

## Mapping status codes to exceptions

Consumer libraries declare a per-operation table and use `map_error`
after each send:

```python
from dexpace.sdk.core.errors import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    map_error,
)

ERROR_MAP = {
    404: ResourceNotFoundError,
    409: ResourceExistsError,
    412: ResourceModifiedError,
}

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
