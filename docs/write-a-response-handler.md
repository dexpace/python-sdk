# Writing a response handler

A `ResponseHandler` is the last hop of a call: it takes the `Response` a
transport produced and turns it into the caller's result — a decoded model, raw
bytes, `None` for a `HEAD`, a streaming iterator — or raises. `core` ships two
handlers built around one pure decision, and you write your own when a call
needs to produce something other than "decode the 2xx body into a type".

## The seam

```python
from __future__ import annotations

from typing import Any

from dexpace.sdk.core.http.response import Response
from dexpace.sdk.core.serde import ResponseHandler, TypeRef


class RawBytesHandler(ResponseHandler):
    """Return the response body as raw bytes, ignoring the requested type."""

    def __call__(self, response: Response, response_type: type[Any] | TypeRef[Any], /) -> Any:
        try:
            body = response.body
            return body.bytes() if body is not None else b""
        finally:
            response.close()
```

The call is **positional-only** (`__call__(response, response_type, /)`), so a
handler stays interchangeable regardless of how it names its parameters, and the
return type is deliberately `Any` — a raw-download handler is just as valid a
`ResponseHandler` as a decoding one. A handler that decodes nothing may ignore
`response_type` entirely.

## The pure decision the built-ins share

`classify_status` is the single pure classification the shipped handlers are
built over. Given a status it returns a `ResponseDisposition` and touches
nothing else — it never reads the body, closes the response, or raises:

```python
from dexpace.sdk.core.serde import ResponseDisposition, classify_status


def is_decodable(response: Response) -> bool:
    return classify_status(response.status) is ResponseDisposition.SUCCESS
```

The three-way outcome keys off the status band (**HTTP-11** — the band
classification `is_success` / `is_error` this reads):

- `SUCCESS` (2xx) — decode the body into the target type.
- `ERROR` (4xx/5xx) — raise the mapped typed error.
- `UNEXPECTED` (1xx/3xx) — neither a decodable success nor a mapped error; a
  success-only handler raises a status-led error.

The two shipped handlers act on that decision. `DecodeBody` decodes the body
into the target type regardless of status, closing the response on every path.
`DecodeSuccess` dispatches on `classify_status`: a 2xx delegates to `DecodeBody`,
a 4xx/5xx raises the mapped error through a `StatusErrorMap`, and a 1xx/3xx
raises a status-led error. Build a custom handler by reusing `classify_status`
for the decision and delegating the decode to a `DecodeBody` you hold, so the
"decode a 2xx body" behaviour is defined once.

## Contract checklist

- **Close the response on every path** — clean return, decode failure, and
  raised error alike. The handler owns the response's lifetime; a leaked body
  pins a transport connection. `response.body.bytes()` / `.string()` already
  consume and close the body, but call `response.close()` in a `finally` so an
  early raise still releases it.
- **Positional-only call signature.** Keep `__call__(response, response_type, /)`
  so your handler drops in wherever a `ResponseHandler` is expected.
- **Respect the status bands (HTTP-11).** If your handler distinguishes success
  from error, drive that split through `classify_status` rather than
  hand-rolling status arithmetic, so a vendor or out-of-range status classifies
  the same way everywhere.
- **Raise the right taxonomy.** A 4xx/5xx should raise an `HttpResponseError`
  (typically via a `StatusErrorMap`, which buffers a bounded, replayable copy of
  the error body); a decode failure raises `DeserializationError`. Do not invent
  a parallel error shape — see [`errors.md`](errors.md).
- **Single-consumption discipline.** Read the body once. If you need repeatable
  reads (logging plus decode), wrap it in a `LoggableResponseBody` rather than
  reading twice — a plain `ResponseBody` is single-use.

## Certifying your handler

Run the response-handler conformance suite, which exercises `classify_status`,
the 2xx/4xx/5xx/1xx-3xx dispatch, the close-on-every-path guarantee, and the
mapped-error path through a `StatusErrorMap`:

```bash
pytest packages/dexpace-sdk-core/tests/serde/test_response_handler.py
```

Add a case for your handler there, asserting that it closes the response on both
the success and the raising path, so its lifecycle discipline is held to the
same bar as the shipped handlers.
