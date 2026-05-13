# dexpace-sdk-http-aiohttp

An [`aiohttp`](https://docs.aiohttp.org/) -backed async HTTP transport for
[`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/).

`AiohttpHttpClient` implements the `AsyncHttpClient` Protocol from `core`,
delegating to `aiohttp.ClientSession`. Streaming uploads are supported via
async-iterable bodies; downloads stream lazily through `AsyncResponseBody`.

`aiohttp` has no sync API, so this package ships an async transport only.
Pair it with `AsyncPipeline` and the async policy stack.

## Installation

```bash
pip install dexpace-sdk-http-aiohttp
```

This pulls in `dexpace-sdk-core` and `aiohttp>=3.9,<4.0` as required
dependencies.

## Usage

```python
import asyncio

from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.http.aiohttp import AiohttpHttpClient


async def main() -> None:
    async with AiohttpHttpClient() as client:
        response = await client.execute(
            Request(method=Method.GET, url=Url.parse("https://api.example.com/v1/status"))
        )
        async with response.body as body:
            print(await body.string())


asyncio.run(main())
```

## Error mapping

aiohttp's transport exceptions surface as the SDK's typed errors:

| aiohttp / asyncio                  | dexpace SDK                       |
| ---                                | ---                               |
| `aiohttp.ClientConnectorError`     | `ServiceRequestError`             |
| `asyncio.TimeoutError`             | `ServiceResponseTimeoutError`     |
| Other `aiohttp.ClientError`        | `ServiceRequestError`             |
| Response parse / decode failures   | `ServiceResponseError`            |

## Dependency relationship

`dexpace-sdk-http-aiohttp` depends on `dexpace-sdk-core`; installing
this package brings it in transitively.
