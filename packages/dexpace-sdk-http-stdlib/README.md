# dexpace-sdk-http-stdlib

Reference HTTP transports for [`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/),
built on the Python stdlib (`urllib.request` and `asyncio`). Zero
third-party dependencies.

Intended for tests, examples, and demonstrating the pipeline shape —
production deployments should plug in an adapter built on a real HTTP
library (`httpx`, `requests`, `aiohttp`) via the dedicated transport
packages.

## Installation

```bash
pip install dexpace-sdk-http-stdlib
```

This pulls in `dexpace-sdk-core` as a required dependency.

## Usage

`UrllibHttpClient` implements the `HttpClient` Protocol from `core` and
plugs straight into a `Pipeline`:

```python
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy
from dexpace.sdk.http.stdlib import UrllibHttpClient

request = Request(
    method=Method.GET,
    url=Url.parse("https://api.example.com/v1/status"),
)

with Pipeline(UrllibHttpClient(), policies=[RetryPolicy(total_retries=3)]) as pipeline:
    with pipeline.run(request, DispatchContext.noop()) as response:
        if response.is_success:
            payload = response.body.string()
```

For async callers, `AsyncioHttpClient` is the equivalent transport —
pair it with `AsyncPipeline` and `RetryPolicy`'s async twin from
`dexpace.sdk.core.pipeline.policies.async_retry`.

```python
from dexpace.sdk.http.stdlib import AsyncioHttpClient
```

## Dependency relationship

`dexpace-sdk-http-stdlib` depends on `dexpace-sdk-core`; installing
this package brings it in transitively.
