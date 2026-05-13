# dexpace-sdk-core

Zero-dependency Python toolkit for building HTTP client libraries.

`dexpace-sdk-core` ships abstractions, models, the request/response
pipeline, authentication primitives, observability hooks, and serde
Protocols — but **not transports**. Pair with a transport package
(`dexpace-sdk-http-stdlib`, `dexpace-sdk-http-httpx`, etc.) or roll your
own `HttpClient` adapter.

## Installation

```bash
pip install dexpace-sdk-core
```

Requires Python 3.12+. No runtime dependencies.

## Usage

Build an immutable `Request`, then run it through a `Pipeline` composed
of policies. The transport is supplied by the caller — any object
implementing the `HttpClient` Protocol (`execute(request) -> Response`)
will do.

```python
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy

request = Request(
    method=Method.POST,
    url=Url.parse("https://api.example.com/v1/resource"),
    headers=Headers([(CONTENT_TYPE, "application/json")]),
    body=RequestBody.from_string(
        '{"key": "value"}',
        media_type=common_media_types.APPLICATION_JSON,
    ),
)

transport: HttpClient = ...  # caller-supplied, e.g. UrllibHttpClient

with Pipeline(transport, policies=[RetryPolicy(total_retries=3)]) as pipeline:
    with pipeline.run(request, DispatchContext.noop()) as response:
        if response.is_success:
            payload = response.body.string()
```

`Request`, `Response`, and friends are frozen dataclasses — mutate
non-destructively via `dataclasses.replace` or the `with_*` helpers.
See the workspace root README for the full architecture overview.

## Related packages

- [`dexpace-sdk-http-stdlib`](https://pypi.org/project/dexpace-sdk-http-stdlib/) —
  reference stdlib-only transports (`UrllibHttpClient`, `AsyncioHttpClient`)
  that depend on this package.
