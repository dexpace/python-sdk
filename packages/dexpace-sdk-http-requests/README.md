# dexpace-sdk-http-requests

Synchronous HTTP transport for
[`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/) built on
the [`requests`](https://pypi.org/project/requests/) library.

`RequestsHttpClient` implements the `HttpClient` Protocol from `core` and
wraps a `requests.Session`. Responses stream via `Response.iter_content`
(`stream=True`), exposed to callers as a `ResponseBody`. Request bodies
are produced via `RequestBody.iter_bytes(8192)`.

## Installation

```bash
pip install dexpace-sdk-http-requests
```

Pulls in `dexpace-sdk-core` and `requests` as required dependencies.

## Usage

```python
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.http.requests import RequestsHttpClient

request = Request(
    method=Method.GET,
    url=Url.parse("https://api.example.com/v1/status"),
)

with Pipeline(RequestsHttpClient(), policies=[]) as pipeline:
    with pipeline.run(request, DispatchContext.noop()) as response:
        if response.is_success:
            payload = response.body.string()
```

## Exception mapping

| `requests` exception   | SDK exception                  |
|------------------------|--------------------------------|
| `ConnectTimeout`       | `ServiceRequestTimeoutError`   |
| `ReadTimeout`          | `ServiceResponseTimeoutError`  |
| `ConnectionError`      | `ServiceRequestError`          |
| `RequestException`     | `ServiceRequestError`          |
