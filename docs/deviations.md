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

**IMPORTANT — this is not an async-only gap.** A separate task built the
equivalent battery against the SYNC `RedirectPolicy`
(`packages/dexpace-sdk-core/tests/pipeline/test_sync_redirect_battery.py`) and
confirmed the same three dimensions are unmet there too, since `AsyncRedirectPolicy`
delegates every per-hop decision to the sync policy rather than restating its
own logic — there is only one resolution core, and it is wired into
`default_pipeline()` **by default, right now**. Concretely, as shipped:
an HTTPS `https://` request that gets redirected to `http://` is followed rather
than refused, and a cross-origin hop does not strip `Cookie` /
`Proxy-Authorization` (only `Authorization`). This is a live gap in the default
SYNC pipeline, not a theoretical one gated behind an async opt-in — it is
ledgered here rather than under a separate entry because it is the exact same
root cause and the exact same fix closes both. The sync battery's three
corresponding tests are also committed as `xfail(strict=True)`, so a fix to the
shared core flips both suites at once.

## `furl` is a sanctioned runtime dependency, not a violation of "stdlib-only"

**Conflict.** The toolkit's stated rule is that `dexpace-sdk-core` ships against
the standard library and nothing else — no third-party runtime dependency. Yet
the built package declares exactly one: `furl` (with its own transitive
`orderedmultidict` / `six`). Read naively, that is the rule being broken in the
first line of `[project].dependencies`.

**Decision.** `furl` stays, as a single deliberate, audited exception, confined
to one module: `packages/dexpace-sdk-core/src/dexpace/sdk/core/http/common/url.py`,
where it powers `Url` parse/serialise. Nothing else in `core` — and nothing in
any transport adapter — may import it directly. The confinement is not a
convention on the honour system; it is machine-enforced by two checked-in gates:

- The import-linter `furl-confinement` contract in `.importlinter` (a
  `forbidden` contract listing `furl` and `orderedmultidict` as forbidden
  import targets from every root package, with a single `ignore_imports` escape
  hatch for `dexpace.sdk.core.http.common.url -> furl`). Run via
  `uv run lint-imports`. Any second module that imports `furl` directly fails
  the contract.
- The dependency-budget audit `tools/check_dependency_audit.py`, which parses
  each distribution's declared `[project].dependencies` and fails if `core`
  declares any third-party runtime dependency other than `furl` (its
  `_CORE_ALLOWED` set is exactly `{"furl"}`), or if an adapter declares more
  than one third-party HTTP library.

**Why.** `Url` needs RFC-correct parse/normalise/serialise behaviour that the
stdlib's `urllib.parse` does not give cleanly, and reimplementing a URL library
by hand is a larger correctness liability than depending on a mature one behind
a boundary. The rule's real intent is "no *unaudited, unbounded* dependency
creep," not "zero dependencies at any cost." Pinning the exception to one module
and enforcing that with two independent tools satisfies the intent while keeping
the surface auditable: the blast radius of `furl` is one file, and adding a
second runtime dependency is a red build, not a code-review judgment call. This
entry exists so a reader who sees `furl` in the manifest and the "stdlib-only"
claim in `CLAUDE.md` does not read them as a contradiction — the exception is
the design, and the two gates are where it is proven.

## Double-consuming a single-use body raises `RuntimeError`, not a bespoke exception

**Conflict.** Stream- and iter-backed bodies (`RequestBody.from_stream` /
`from_iter`, their async twins, and the stream-backed `ResponseBody`) are
single-use: consuming them a second time must fail loudly rather than silently
re-emit an exhausted stream. A design that leans on exception *types* for
control flow would want a dedicated, catchable class here — say a
`BodyAlreadyConsumedError` in the `errors` hierarchy — so callers could branch
on it.

**Decision.** The second consumption raises a plain `RuntimeError` carrying a
specific, actionable message — not a bespoke exception type. The guard itself
lives in `packages/dexpace-sdk-core/src/dexpace/sdk/core/http/_once_guard.py`
(`_OnceGuard.claim`, a lock-protected single-claim latch shared by both body
sides); the messages are defined next to the bodies that raise them, e.g.
`_STREAM_SINGLE_USE_MSG` / `_ITER_SINGLE_USE_MSG` in
`packages/dexpace-sdk-core/src/dexpace/sdk/core/http/request/request_body.py`,
with parallel constants in `async_request_body.py` and the response-body
modules. Each message names the cause and the fix ("Call `to_replayable()`
BEFORE the first `iter_bytes()` …").

**Why.** Re-consuming an exhausted single-use body is a *programming* error, not
a recoverable runtime condition a caller should catch and branch on — the
correct response is always to fix the call site (buffer via `to_replayable()`
before the first send, or clone the request), never to handle the exception. A
bespoke type would invite `except BodyAlreadyConsumedError:` handlers that paper
over the bug instead of fixing it, and would add public surface (`__all__`
entries, docs, import paths) to encode a distinction with no legitimate consumer.
`RuntimeError` with a precise message is the deliberately simpler shape: it
surfaces the mistake at the exact call that made it, reads clearly in a
traceback, and stays out of the catchable-error hierarchy. This is a chosen
simplification, not an unfinished error-taxonomy — do not "upgrade" it to a
custom class without a concrete caller that must distinguish it programmatically.

## Sync cooperative cancellation stops at the backoff boundary, not mid-read

**Conflict.** The async stack can abandon an in-flight operation promptly:
cancelling the awaiting task unwinds it at the next `await`. A reader might
expect the sync stack to offer the same "cancel now" immediacy — for a blocked
retry to be interrupted the instant a caller signals cancellation. It does not,
and cannot, for a read that is already blocked in the transport.

**Decision.** Sync cooperative cancellation is a documented *limit*, not a bug.
The interruptible-sleep primitive exists —
`packages/dexpace-sdk-core/src/dexpace/sdk/core/util/clock.py` ships
`CancellationToken`, a `threading.Event`-backed handle whose `sleep(duration)`
returns early the moment another thread calls `cancel()`. That makes a *waiting*
period (a backoff delay) promptly interruptible. But the sync `RetryPolicy`
(`packages/dexpace-sdk-core/src/dexpace/sdk/core/pipeline/policies/retry.py`)
paces its own backoff through `Clock.sleep` (`_sleep_bounded`), and — more to
the point — a request that is already blocked in a socket read on a worker
thread cannot be pre-empted at all: CPython threads are not interruptible from
outside, so an in-flight blocking read runs to its own socket timeout before the
retry loop regains control and can observe cancellation.

**Why.** This is a property of the runtime, not a missing feature. There is no
portable, safe way to interrupt a thread parked in a blocking C-level `recv`;
the only correct lever is the transport's own read timeout, which bounds how
long that read can block. So the honest contract is: on the sync path,
cancellation and deadlines are honoured at attempt boundaries and during backoff
waits (where `CancellationToken` / deadline clamping apply), and an already-issued
blocking read is bounded by its socket timeout rather than interrupted instantly.
Callers who need tighter mid-read cancellation should set an aggressive transport
read timeout or use the async stack, where task cancellation unwinds at the next
`await`. Ledgered here so the absence of mid-read sync cancellation reads as a
deliberate boundary of the threading model, not an oversight in the retry policy.

## The async pipeline ships without wire logging or a per-attempt tracing span

**Conflict.** `default_pipeline()` wires two per-attempt observability policies —
`LoggingPolicy` (request/response wire log with a body preview) and `TracingPolicy`
(a per-attempt OpenTelemetry span). Parity would have `default_async_pipeline()`
carry async twins of both. It does not: the async stack keeps per-operation
tracing (`AsyncOperationTracingPolicy`) but omits per-attempt wire logging and
the per-attempt span entirely.

**Decision.** Ledgered as a tracked, deliberate gap — not silently dropped. Both
omissions are documented at the point of use: the "Known gap — async wire
logging" paragraph on `default_async_pipeline()`'s docstring in
`packages/dexpace-sdk-core/src/dexpace/sdk/core/pipeline/defaults.py`, and the
"per-attempt OpenTelemetry span machinery is sync-only and has no async
counterpart" note on `TracingPolicy` in
`packages/dexpace-sdk-core/src/dexpace/sdk/core/pipeline/policies/tracing_policy.py`.
Correlation ids still propagate across `await` boundaries (bound by
`AsyncOperationTracingPolicy`), so an async caller's own log records stay
correlated; only the SDK's built-in per-attempt wire log and span are absent.

**Why.** The sync `LoggingPolicy` and per-attempt `TracingPolicy` were written
against synchronous body iteration and a synchronous span lifecycle; a correct
async twin is real work (async-safe body-preview capture, span start/stop across
`await` points), not a mechanical rename, and shipping a subtly wrong async
logger is worse than shipping none. Rather than block the async stack on that
work or fake parity, the gap is made explicit at the two docstrings above and
recorded here, with the documented escape hatch: an async caller who needs
request/response logging wraps the transport or adds a custom `AsyncPolicy`. An
async logging policy is planned for a later milestone; until it lands this stays
a known, opt-out-able gap rather than a defect.

## The reference stdlib transport accepts capability gaps its siblings don't

**Conflict.** The `HttpClient` / `AsyncHttpClient` Protocols promise a uniform
send surface, and the `httpx` / `aiohttp` / `requests` adapters back it with
full-featured libraries (streaming uploads, per-phase timeouts, repeated
headers). The stdlib transports in `dexpace-sdk-http-stdlib`
(`UrllibHttpClient`, `AsyncioHttpClient`) implement the same Protocol but cannot
match those capabilities, so identical calling code behaves differently
depending on which transport is plugged in.

**Decision.** The stdlib transport is a *reference*, zero-third-party-dependency
implementation that deliberately accepts a bounded set of capability gaps rather
than reimplementing a production HTTP stack on top of `urllib` / raw asyncio
sockets. The gaps are enumerated in the module docstrings —
`packages/dexpace-sdk-http-stdlib/src/dexpace/sdk/http/stdlib/urllib_http_client.py`
and `.../asyncio_http_client.py`: no streaming uploads (the body is fully
buffered before send), coarse single-value timeouts (no connect/read/write
granularity), and constrained multi-value request-header handling (`urllib`
flattens repeats with `", "`; the asyncio client emits repeated lines). The
asyncio client additionally speaks only HTTP/1.1 with `Connection: close` and
rejects `Transfer-Encoding: chunked` responses loudly rather than truncating.

**Why.** The point of the stdlib transport is to give the toolkit a working,
dependency-free transport for tests, examples, and constrained environments —
its zero-dependency posture is the feature, and matching a production library's
capability surface would mean rebuilding one, which defeats the purpose and
would need a third-party dependency the package exists to avoid. Each docstring
tells the reader exactly which limitation to escape by plugging in `httpx` /
`aiohttp` / `requests`. Ledgered here so the divergence between transports reads
as a deliberate reference-vs-production split, not a bug in the stdlib adapter.

## How this ledger feeds the traceability audit

This file is written to be the human-readable source of truth for intentional
deviations and not-applicable (N/A) requirements consumed by the traceability
audit in `tools/traceability_audit.py`. The audit already exposes the seam: both
`audit(...)` and `TraceabilityPlugin` accept an `na_ids` set (requirement ids
that are intentionally not implemented and therefore never counted as coverage
gaps), and `na_ids_from_file(path)` reads that set from a newline-delimited id
file (one id per line; blank lines and `#` comments ignored; a missing file
yields an empty set), pointed at by the `DEXPACE_TRACEABILITY_NA` environment
variable.

That reader is, by its own docstring, the *placeholder* seam for this ledger: it
consumes a flat id file, **not** this Markdown document, and the real requirement
index it audits against is still `PLACEHOLDER_INDEX`, not the finished spec. So
the wiring that would derive N/A requirement ids from these prose entries — and
the requirement ids the entries would cite once the real index exists — is
deliberately **not** built here; it belongs to the later task that lands the real
requirement index and the id-extraction step. This entry records that the
integration point exists and is intentionally a placeholder, so a future
implementer knows where the ledger plugs in (`na_ids` via `na_ids_from_file`)
without this docs-only change inventing audit code outside its scope.
