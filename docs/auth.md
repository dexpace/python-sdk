# Authentication

The `http.auth` package ships credential types, authentication
policies, and a pluggable token cache.

## Credentials

| Class                    | Use                                                                |
|--------------------------|--------------------------------------------------------------------|
| `KeyCredential`          | Simple API key. `update(key)` rotates without rebuilding the client. |
| `NamedKeyCredential`     | Name + key pair (HMAC-SAS style).                                  |
| `BasicAuthCredential`    | Username + password, base64-encoded once at construction.          |
| `TokenCredential`        | Sync Protocol for OAuth-style providers (`get_token_info`).        |
| `AsyncTokenCredential`   | Async Protocol for OAuth-style providers.                          |

All concrete credentials redact secrets in their `__repr__`.

## Policies

| Policy                       | Purpose                                                   |
|------------------------------|-----------------------------------------------------------|
| `KeyCredentialPolicy`        | Stamp a `KeyCredential` into a configurable header.       |
| `BasicAuthPolicy`            | Stamp `Authorization: Basic <…>`.                         |
| `BearerTokenPolicy`          | Acquire + cache + apply OAuth bearer tokens (sync).       |
| `AsyncBearerTokenPolicy`     | Async twin.                                               |

`BearerTokenPolicy`:

- Enforces HTTPS by default (opt out per call with
  `ctx.options["enforce_https"] = False`).
- Caches the most recent `AccessTokenInfo` in a `TokenCache` (default:
  `InMemoryTokenCache`).
- Calls `AccessTokenInfo.needs_refresh()` before each request; refreshes
  proactively when `refresh_on` has passed or `expires_on - leeway`
  (default 300 s) has been reached.
- On a 401 response, invalidates the cached token — but only when that
  token still matches the `Authorization` header this request carried, so
  a stale rejection cannot discard a token a concurrent refresh already
  replaced. A 401 without a `WWW-Authenticate` header is then surfaced
  unchanged. When the header is present, `on_challenge(request, response)`
  is consulted; override it in a subclass to handle CAE / claims-challenge
  flows. If it authorizes a retry, the request is re-issued at most once
  with a refreshed token — but a single-use (non-replayable) body is
  surfaced un-replayed rather than corrupting a half-consumed stream. The
  replayed response is returned regardless of its status; there is no
  challenge loop. Both the sync and async policies enforce this identically.

## Token cache

Implementations satisfy the `TokenCache` Protocol:

```python
class TokenCache(Protocol):
    def get(self, scopes, audience=None) -> AccessTokenInfo | None: ...
    def set(self, scopes, token, audience=None) -> None: ...
    def clear(self) -> None: ...
```

`InMemoryTokenCache` is a thread-safe dict-backed implementation,
keyed by the sorted scope list plus optional audience. Pass an instance
to `BearerTokenPolicy(cache=…)` to share tokens across multiple
credentials or policies.
