# The codegen target

This is the contract a code generator targets when it emits an SDK on top of
`dexpace-sdk-core`. The generated code is a thin **projection**: an operation
table plus a facade whose every method binds its arguments into an
`OperationInput` and delegates to a shared executor. All the behaviour — request
assembly, the pipeline, retry, auth resolution, error mapping, pagination, SSE —
lives in `core`, so the generator emits data and delegation, never logic.

The public surface lives in `dexpace.sdk.core.codegen`:

| Symbol | Role |
|--------|------|
| `Operation`, `OperationInput` | The declarative request description (static half + per-call half) |
| `assemble` | Turns an `Operation` + `OperationInput` into a concrete `Request` |
| `ServiceCore`, `AsyncServiceCore` | The two platform executors a facade delegates to |
| `StatusErrorMap` | Frozen status-code → typed-error mapping |
| `AsyncResponseHandler`, `AsyncDecodeSuccess` | Async twins of serde's `ResponseHandler` / `DecodeSuccess` |
| `ServiceDefaults`, `merge_config`, `merge_async_config` | The Tier-2 config-folding seam |
| `AuthRequirement`, `AuthDescriptor`, `AuthRegistry`, `AuthScheme`, `AuthResolutionError` | The tiered auth-requirement resolver |

The [`examples/petstore`](../examples/petstore) package is the compiling witness
for everything below: a generated SDK rendered from a frozen OpenAPI document,
with a regen-diff test that fails on any drift and an end-to-end canary
(`examples/petstore/tests/test_petstore_canary.py`) that drives the whole stack
over an in-memory transport.

## Request assembly

An `Operation` is the static, reusable half of a request shape; an
`OperationInput` supplies the per-call values. `assemble` combines them:

```python
from dexpace.sdk.core.codegen import Operation, OperationInput, assemble
from dexpace.sdk.core.http.request import Method

GET_PET = Operation(name="get_pet", method=Method.GET, path="/pets/{pet_id}")

request = assemble(
    GET_PET,
    OperationInput(path_params={"pet_id": "7"}),
    "https://api.example.com",
)
```

Both value objects are frozen, keyword-only dataclasses, so a future minor
version can append a field without breaking a positional call site — the
forward-compatibility guarantee a generated client depends on. The assembly
rules are normative:

- **Path templates** substitute `{placeholder}` tokens from
  `OperationInput.path_params`. Each value is percent-encoded with `safe=""`, so
  a literal `/` becomes `%2F` and stays inside one path segment. A placeholder
  with no matching key raises `KeyError`, reporting only the missing name (never
  the supplied values, so a signed token cannot leak).
- **The base URL's query is preserved** (a SAS / signed-URL token survives), and
  `OperationInput.query` is appended after it. The base URL must not carry a
  fragment — `assemble` raises `ValueError` if it does.
- **Headers overlay:** per-call `OperationInput.headers` replace the operation's
  static headers by name; names only on the operation are kept.
- **`name` is advisory.** It stamps tracing context for readability and never
  influences assembly, routing, or any cache key; it is excluded from equality
  and hashing.
- **The body is carried through verbatim.** This layer does not serialize it;
  the `serde` argument is reserved for a later executor hop.

## The executors

A generated facade holds a `ServiceCore` (sync) or `AsyncServiceCore` (async)
and delegates every method to it. Construct an executor from exactly one of a
transport or a caller-supplied pipeline:

```python
from dexpace.sdk.core.codegen import ServiceCore

# Owns the pipeline it builds around the transport — closing the core closes it.
core = ServiceCore(base_url="https://api.example.com", transport=client)

# Borrows a caller-supplied pipeline — never closes it. Bake in auth this way.
core = ServiceCore(base_url="https://api.example.com", pipeline=my_pipeline)
```

Passing a `transport` builds the canonical `default_pipeline` around it and
*owns* it; passing a `pipeline` borrows it and never closes it. Ownership-aware
close is the rule: closing the executor closes an owned transport but leaves a
borrowed pipeline untouched (`examples/petstore` exercises both).

Each executor exposes four entry points:

- `execute(op, input, *, response_type=..., handler=..., auth=..., **call_options)`
  runs one operation and decodes the result. With a `response_type` the default
  `DecodeSuccess` handler decodes a 2xx body into that type; with neither a
  `response_type` nor a `handler` the raw `Response` is returned for the caller
  to consume and close; a `handler` overrides the default.
- `execute_request(request, ...)` is the raw-call seam: it runs a pre-built
  `Request` as-is (no operation tier for auth).
- `paginate(op, input, strategy, *, max_pages=..., ...)` returns the certified
  `Paginator` / `AsyncPaginator`, so the wire-exact next-request splice and
  close-ledger guarantees carry through unchanged. Each page fetch runs the full
  pipeline.
- `events(op, input, mapper, ...)` returns the certified typed SSE connection
  with a mapper applied, leaving the core SSE layer free of any
  service-specific sentinel or serde convention.

The default handler is `DecodeSuccess` (sync) / `AsyncDecodeSuccess` (async),
wired with the executor's `StatusErrorMap` and `serde`. Per-call keyword
arguments beyond the named ones flow through to the pipeline's policies via
`ctx.options` (a `retry_total` override, `logging_enabled=False`, and so on).

## Error mapping

`StatusErrorMap` is the declarative half of a generated client's error
handling: a frozen status-code → typed-error mapping, read as data so a service
SDK never grows a hand-written `if status == ...: raise` chain.

```python
from typing import Any

from dexpace.sdk.core.codegen import StatusErrorMap
from dexpace.sdk.core.errors import HttpResponseError
from dexpace.sdk.core.http.response import Status


class PetStoreError(HttpResponseError[Any]):
    """Base for every typed petstore response error."""


class PetNotFoundError(PetStoreError):
    """Raised for a 404."""


PETSTORE_ERRORS = StatusErrorMap(
    by_status={Status.NOT_FOUND: PetNotFoundError},
    default=PetStoreError,
)
```

The map spans the **response branch only**: every registered class must extend
`HttpResponseError` and must **not** extend `OSError`, enforced at construction
(`TypeError` otherwise). That is the hard rule a service taxonomy relies on — a
service-specific error can never masquerade as a transport-level `NetworkError`,
so an `except OSError:` site keeps matching transport failures unchanged. On a
4xx/5xx response, `raise_for` constructs the mapped error (the per-status
override, else `default`), buffers a bounded, replayable copy of the error body
onto it, and raises; `for_status` is the non-raising peek. See
[`errors.md`](errors.md).

## Tiered auth resolution

Auth requirements arrive at three tiers, most specific first: **per-call** (the
`auth=` argument), then **operation-level** (declared on the `Operation`), then
**client-level** (configured on the executor). A generated operation declares
what it needs as an `AuthDescriptor` — a non-empty, ordered list of
`AuthRequirement` alternatives (OR semantics, first satisfiable wins):

```python
from dexpace.sdk.core.codegen import AuthDescriptor, AuthRequirement, Operation
from dexpace.sdk.core.http.request import Method

GET_PET = Operation(
    name="get_pet",
    method=Method.GET,
    path="/pets/{pet_id}",
    auth=AuthDescriptor.of(AuthRequirement("bearer", ("pets:read",))),
)
```

The executor resolves that declaration against an `AuthRegistry` of
`AuthScheme`s (a scheme pairs a name and grantable scopes with the concrete
stamping policy that realises it). Resolution is deliberately strict:

- **Presence selects the tier**, not satisfiability. The most specific *present*
  tier is chosen.
- **The selected tier must then be satisfiable.** If it is present but no
  registered scheme satisfies any of its alternatives, resolution raises
  `AuthResolutionError` — it never falls through to a broader tier. A caller who
  asked for a specific per-call credential that cannot be honoured gets a loud
  failure, never a silent downgrade to the client default (which would
  authenticate with the wrong credential).

The resolver only *selects* a policy; the stamping is done downstream by the M4
auth policies (`BearerTokenPolicy`, `KeyCredentialPolicy`, `BasicAuthPolicy`).
The petstore canary exercises all three outcomes: the operation tier winning
over a client default, falling back to the client default when an operation
declares none, and failing loudly when a present tier is unsatisfiable (with no
request sent).

## Service defaults (the Tier-2 seam)

`ServiceDefaults` lets a generated SDK bake in its own defaults — base URL,
product User-Agent token, api-version stamp, error map, wire serde — and
`merge_config` / `merge_async_config` fold those, plus an optional caller
override of the same shape, into a single core pipeline's constructor kwargs:

```python
from dexpace.sdk.core.codegen import ServiceCore, ServiceDefaults, merge_config

DEFAULTS = ServiceDefaults(
    base_url="https://api.example.com",
    user_agent_token="acme-sdk/2.3",
    api_version="2026-01-01",
)

core = ServiceCore(**merge_config(DEFAULTS, transport=client))
```

The folding rules are normative:

- **Field-wise precedence.** A non-`None` field on the caller override wins; a
  non-`None` field on the service defaults wins next; the toolkit's own
  fallbacks apply only when both leave a field unset. Overriding just `base_url`
  still inherits the service's `user_agent_token`.
- **`base_url` is required** — one of the two layers must resolve it, or
  `merge_config` raises `ValueError`.
- **One pipeline, never a wrapper.** `merge_config` makes exactly one
  `default_pipeline(...)` call, folding the User-Agent token in as the leading
  `client_identity` token (prepended to the platform and runtime tokens) and the
  api-version stamp in as an appended policy re-applied on every redirect hop and
  retry attempt. It never builds a second pipeline or nests one client in
  another. A caller-supplied already-built `pipeline` passes through untouched —
  service defaults cannot be layered onto an opaque pipeline, so use
  `transport=` when the api-version stamp or User-Agent token must apply.

`merge_config` returns a mapping ready to splat into
`ServiceCore(**merge_config(...))`; `merge_async_config` is the async twin for
`AsyncServiceCore`.

## The generated shape, end to end

Putting it together, a generated SDK is three small pieces (see
`examples/petstore/src/dexpace_petstore/`):

1. **An operation table** (`_generated/operations.py`) — pure data, one
   `Operation` per API operation, imported by both facades.
2. **Projection facades** (`_generated/sync_client.py`,
   `_generated/async_client.py`) — each method binds arguments into an
   `OperationInput` and calls `execute` / `paginate` / `events`, carrying no
   logic of its own.
3. **A hand-authored runtime shim** (`models`, `errors`, `_support`) — the typed
   models, the `StatusErrorMap`, and the body / pagination / SSE binders the
   facades reference.

Because the facades are projections and the executor is shared, the certified
guarantees of `core` — wire-exact pagination, reconnecting SSE, the retry and
redirect batteries, the tiered auth resolver — reach the generated caller
unchanged. The petstore canary is the standing proof that they do.
