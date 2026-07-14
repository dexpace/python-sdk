# Deviations

Records deliberate deviations from the design intent — places where the shipped
behaviour intentionally differs from a rule or a sibling implementation, together
with the conflict that forced the choice, the decision taken, and the rationale.
Each entry is self-contained so a reader with no prior context understands why the
code looks the way it does.

## Async redirect policy is not wired into `default_async_pipeline()`

**Conflict.** The sync `default_pipeline()` wires `RedirectPolicy` by default, and
an async twin (`AsyncRedirectPolicy`) exists and was originally wired into
`default_async_pipeline()` for parity. That collides with the safety rule that an
async pipeline must not silently follow redirects unless the redirect path is
proven safe against the full redirect security battery:

- loop / max-hops / malformed-`Location` all hand the current response back
  unfollowed and *open* (never closed);
- `Authorization` stripped on every hop, with cross-origin judged against the
  **seed** origin (not the previous hop), additionally stripping `Cookie` and
  `Proxy-Authorization`;
- HTTPS→HTTP downgrade default-denied;
- `303` rebuild strips the body and every `Content-*` header;
- `Location` resolution preserves percent-encoding wire-exactly (no naive
  parse/re-encode round-trip).

**Decision.** Gated off. `AsyncRedirectPolicy` is **removed from
`default_async_pipeline()`'s default construction**; the class stays fully
available and is wired only when a caller opts in via
`default_async_pipeline(client, redirect=AsyncRedirectPolicy(...))` or by adding
it to a custom pipeline. The gap and the opt-in are documented on the factory's
docstring.

**Why.** The battery was written and run against the async twin (see
`packages/dexpace-sdk-core/tests/pipeline/test_async_redirect_battery.py`). The
twin delegates every per-hop decision to the wrapped sync `RedirectPolicy`, so it
inherits that policy's current limitations. At the time the gate was written it
failed four dimensions of the battery:

- HTTPS→HTTP downgrades are followed rather than default-denied;
- `Cookie` and `Proxy-Authorization` are not stripped on a cross-origin hop
  (only `Authorization` is);
- cross-origin is judged against the previous hop rather than the seed origin;
- ~~`Location` resolution round-trips through a parse/re-encode that rewrites
  percent-encoding (for example `%20` in a query becomes `+`).~~ **Fixed** by
  the pure RFC 3986 query codec (`PY-M2-02`, `_query_codec.py`), landed after
  this gate — `Url` no longer round-trips through `furl`'s serializer for the
  wire-exact rendering path, so `Location` resolution now preserves
  percent-encoding exactly. `test_location_resolution_is_wire_exact` passes
  and its `xfail` marker was removed.

One dimension that was outright broken — a malformed `Location` raised
`ValueError` and leaked the in-hand response — was fixed at the source in the
shared `RedirectPolicy` (it now returns the current response unfollowed and open,
matching loop / max-hops), so both the sync and async paths are correct there.
Three gaps remain (HTTPS→HTTP downgrade, cross-origin Cookie/Proxy-Authorization
stripping, seed-origin cross-origin judgment) — behavioural/feature work on the
shared resolution logic; until that logic is certified against the full battery,
silently following redirects on the async path is the failure this gate avoids.
The battery's remaining unmet dimensions stay committed as `xfail(strict=True)`
executable specs, so each flips to a hard failure as its underlying logic is
fixed — forcing this gate to be revisited, and the twin re-wired into
`default_async_pipeline()`, once all three close.
