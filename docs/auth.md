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
- On any 401 response, invalidates the cached token. If the response
  also carries a `WWW-Authenticate` header, then calls
  `on_challenge(request, response)`. Override `on_challenge` in a
  subclass to handle CAE / claims-challenge flows.

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
