# Request / Response bodies

`RequestBody` and `ResponseBody` are the typed body abstractions. They
expose `iter_bytes(chunk_size)` as the primary streaming surface and
classmethod factories for the common shapes.

## Single-use vs replayable

| Factory                                  | Replayable | Notes                                               |
|------------------------------------------|------------|-----------------------------------------------------|
| `RequestBody.from_bytes(data)`           | ✅         | Backed by an immutable `bytes`.                     |
| `RequestBody.from_string(s)`             | ✅         | Encoded once at construction.                       |
| `RequestBody.from_form(fields)`          | ✅         | `application/x-www-form-urlencoded`.                |
| `RequestBody.from_file(path)`            | ✅         | Re-opens the file each call. `FileRequestBody`.     |
| `RequestBody.from_multipart(parts)`      | ✅         | Rendered once at construction with stable boundary. |
| `RequestBody.from_stream(BinaryIO)`      | ❌         | Single-use; call `to_replayable()` first if retries are needed. |
| `RequestBody.from_iter(Iterable[bytes])` | ❌         | Same. The iterable is consumed on first `iter_bytes`. |

Single-use bodies raise `RuntimeError` on the second `iter_bytes` call.
The retry policy in `pipeline.policies.retry` **does** automatically
buffer single-use bodies when the effective per-call retry total is
positive: `RetryPolicy.send` merges any `retry_total` override in
`ctx.options` with the policy's `total_retries` default, then calls
`body.to_replayable()` before the first attempt whenever that merged
total is greater than zero. A per-call `retry_total=3` over a
`total_retries=0` policy still buffers for replay, while a per-call
`retry_total=0` over a retrying policy skips the buffering step. If you
bypass the retry policy you can still call `body.to_replayable()`
yourself before the first send.

## Response shape

```python
with http_client.execute(request) as response:
    if response.is_success:
        text = response.body.string()       # decodes per media-type charset
        # or response.body.bytes() for raw bytes
        # or `for chunk in response.body.iter_bytes(8192): ...` to stream
```

`ResponseBody` is also single-use. Wrap with `LoggableResponseBody`
when repeatable reads are required.

## Body capture for logging

```python
from dexpace.sdk.core.http.request import LoggableRequestBody, RequestBody

inner = RequestBody.from_string("payload")
logged = LoggableRequestBody(inner, max_capture_bytes=8 * 1024)

# Drain to the transport sink; bytes are mirrored into the in-memory tap.
logged.write_to(some_binary_stream)
print(logged.snapshot()[:200])
```

The cap is a soft truncate on the *tap*; the primary write path
receives the full payload. `LoggableResponseBody` is the same shape on
the response side — first access drains the underlying body into a
`bytes` cache that subsequent calls replay.

## Async equivalents

`AsyncRequestBody` / `AsyncResponseBody` mirror the sync APIs with
`aiter_bytes` and `async def bytes()` / `string()`. The factory sets
differ per side: `AsyncRequestBody` exposes `from_bytes`, `from_string`,
`from_form`, `from_async_stream`, and `from_async_iter`;
`AsyncResponseBody` exposes `from_bytes` and `from_async_stream` only
(there is no `from_async_iter` on the response side).
