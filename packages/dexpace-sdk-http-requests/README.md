# dexpace-sdk-http-requests

Synchronous HTTP transport for
[`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/) built on
the [`requests`](https://pypi.org/project/requests/) library.

`RequestsHttpClient` implements the `HttpClient` Protocol from `core` and
wraps a `requests.Session`. Responses stream via `Response.iter_content`
(`stream=True`), exposed to callers as a `ResponseBody`.

Request bodies are framed so the wire request carries exactly one framing
header. A replayable body with a known length (for example `from_bytes` or
`from_file`) is sent as a sized payload, so `requests` adds `Content-Length`
and does not chunk. A streaming or unknown-length body is sent as an iterator
and `requests` applies `Transfer-Encoding: chunked`. The two headers are never
emitted together.

A session you pass in is borrowed: the client never closes it, so a pooled
session shared with other components stays usable. Only a session the client
created itself is closed on `close()`.

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

## Thread safety

The `HttpClient` Protocol expects an implementation safe to call concurrently,
and `RequestsHttpClient` itself holds no per-call mutable state. The underlying
`requests.Session` is the caveat: its cookie jar is not thread-safe, and
concurrent requests through one session can race on it (requests issues
[#1871](https://github.com/psf/requests/issues/1871) and
[#2766](https://github.com/psf/requests/issues/2766)).

If you drive one client from multiple threads and the server sets cookies,
either give each thread its own client/session, or disable cookie persistence
by passing a session with a no-op cookie jar:

```python
import requests
from requests.cookies import RequestsCookieJar
from dexpace.sdk.http.requests import RequestsHttpClient


class _NullCookieJar(RequestsCookieJar):
    def set_cookie(self, cookie, *args, **kwargs):  # noqa: D102
        return None

    def extract_cookies(self, response, request):  # noqa: D102
        return None


session = requests.Session()
session.cookies = _NullCookieJar()
client = RequestsHttpClient(session=session)
```

Cookies are part of HTTP state the pipeline does not depend on, so disabling
persistence is safe for stateless API usage and removes the race entirely.

## License

Licensed under the [MIT License](LICENSE.md).
Copyright © 2026 dexpace and Omar Aljarrah.
