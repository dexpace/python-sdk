# dexpace-sdk-http-httpx

`httpx`-backed HTTP transports for
[`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/).

Ships two transports:

- `HttpxHttpClient` — sync, satisfies the `HttpClient` Protocol from `core`.
- `AsyncHttpxHttpClient` — async, satisfies the `AsyncHttpClient` Protocol.

Unlike the stdlib reference transports (`UrllibHttpClient` /
`AsyncioHttpClient`), `httpx` supports streaming uploads, per-phase
timeouts (`connect` / `read` / `write` / `pool`), HTTP/2 (opt-in via
`httpx`), and proxy configuration.

## Installation

```bash
pip install dexpace-sdk-http-httpx
```

This pulls in `dexpace-sdk-core` and `httpx` as required dependencies.

## Usage

```python
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.http.httpx import HttpxHttpClient

request = Request(
    method=Method.GET,
    url=Url.parse("https://api.example.com/v1/status"),
)

with Pipeline(HttpxHttpClient(connect_timeout=5.0, read_timeout=30.0)) as pipeline:
    with pipeline.run(request, DispatchContext.noop()) as response:
        if response.is_success:
            payload = response.body.string()
```

For async callers, use `AsyncHttpxHttpClient`:

```python
from dexpace.sdk.http.httpx import AsyncHttpxHttpClient
```

## Error mapping

`httpx` exceptions are mapped onto the `dexpace.sdk.core.errors`
hierarchy:

| httpx exception | dexpace error |
|---|---|
| `httpx.ConnectError` | `ServiceRequestError` |
| `httpx.ConnectTimeout` | `ServiceRequestTimeoutError` |
| `httpx.ReadTimeout` | `ServiceResponseTimeoutError` |
| `httpx.WriteTimeout` | `ServiceRequestTimeoutError` |
| `httpx.PoolTimeout` | `ServiceRequestTimeoutError` |
| `httpx.RequestError` (other) | `ServiceRequestError` |
