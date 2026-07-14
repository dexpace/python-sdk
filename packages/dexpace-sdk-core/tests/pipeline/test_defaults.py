# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``default_pipeline`` and ``default_async_pipeline`` factories."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dexpace.sdk.core.http.auth.credentials import KeyCredential
from dexpace.sdk.core.http.auth.policies import KeyCredentialPolicy
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import (
    AsyncPipeline,
    AsyncStagedPipelineBuilder,
    Pipeline,
    PipelineContext,
    Stage,
    StagedPipelineBuilder,
)
from dexpace.sdk.core.pipeline._async_transport_runner import _AsyncTransportRunner
from dexpace.sdk.core.pipeline._transport_runner import _TransportRunner
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.defaults import default_async_pipeline, default_pipeline
from dexpace.sdk.core.pipeline.policies.async_client_identity import AsyncClientIdentityPolicy
from dexpace.sdk.core.pipeline.policies.async_idempotency import AsyncIdempotencyPolicy
from dexpace.sdk.core.pipeline.policies.async_logging_policy import AsyncLoggingPolicy
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy
from dexpace.sdk.core.pipeline.policies.async_set_date import AsyncSetDatePolicy
from dexpace.sdk.core.pipeline.policies.async_tracing_policy import (
    AsyncOperationTracingPolicy,
    AsyncTracingPolicy,
)
from dexpace.sdk.core.pipeline.policies.client_identity import ClientIdentityPolicy
from dexpace.sdk.core.pipeline.policies.idempotency import IdempotencyPolicy
from dexpace.sdk.core.pipeline.policies.logging_policy import LoggingPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy
from dexpace.sdk.core.pipeline.policies.set_date import SetDatePolicy
from dexpace.sdk.core.pipeline.policies.tracing_policy import OperationTracingPolicy, TracingPolicy
from dexpace.sdk.core.pipeline.policy import Policy

from ..conftest import FakeClock

#: One scripted hop: the status to return and an optional ``Location`` header.
type _Hop = tuple[Status, str | None]


class _StubTransport:
    def execute(self, request: Request) -> Response:  # pragma: no cover
        raise NotImplementedError


class _AsyncStubTransport:
    async def execute(self, request: Request) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


class _AsyncFakeClock:
    """Deterministic async clock; ``sleep`` advances simulated time."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)


class _OwnershipTransport:
    """Sync transport mirroring the adapter close contract: ownership-aware + idempotent.

    ``owns`` models a transport adapter that constructed its own underlying
    client (the adapters' ``_owns_client`` flag): ``close`` tears the underlying
    resource down only when the transport owns it, and is idempotent (guarded
    like the adapters' ``_closed`` flag) so a second close never doubles the
    teardown. A borrowed (BYO) resource is left intact — closing the pipeline
    delegates to this method, which is the seam that keeps a caller's transport
    alive.
    """

    def __init__(self, script: Sequence[_Hop] | None = None, *, owns: bool) -> None:
        self._script: list[_Hop] = list(script or [(Status.OK, None)])
        self._owns = owns
        self._closed = False
        self.requests: list[Request] = []
        self.close_calls = 0
        self.underlying_torn_down = False

    def execute(self, request: Request) -> Response:
        status, location = self._script[len(self.requests)]
        self.requests.append(request)
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=status)
        if location is not None:
            response = response.with_header("Location", location)
        return response

    def close(self) -> None:
        self.close_calls += 1
        if self._closed:
            return  # idempotent: a second close is a no-op
        self._closed = True
        if self._owns:
            self.underlying_torn_down = True


class _AsyncOwnershipTransport:
    """Async twin of ``_OwnershipTransport`` (tears down via ``aclose``)."""

    def __init__(self, script: Sequence[_Hop] | None = None, *, owns: bool) -> None:
        self._script: list[_Hop] = list(script or [(Status.OK, None)])
        self._owns = owns
        self._closed = False
        self.requests: list[Request] = []
        self.aclose_calls = 0
        self.underlying_torn_down = False

    async def execute(self, request: Request) -> AsyncResponse:
        status, location = self._script[len(self.requests)]
        self.requests.append(request)
        response = AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=status)
        if location is not None:
            response = response.with_header("Location", location)
        return response

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self._closed:
            return  # idempotent: a second close is a no-op
        self._closed = True
        if self._owns:
            self.underlying_torn_down = True


class _OptionsProbe(Policy):
    """Records the ``ctx.options`` mapping seen at the transport boundary."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.seen.append(ctx.options)
        return self.next.send(request, ctx)


class _AsyncOptionsProbe(AsyncPolicy):
    """Async twin of ``_OptionsProbe``."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        self.seen.append(ctx.options)
        return await self.next.send(request, ctx)


class _StubAsyncAuth(AsyncPolicy):
    """Minimal async credential policy occupying the AUTH pillar for introspection."""

    STAGE = Stage.AUTH
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return await self.next.send(request, ctx)


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _dispatch(trace: str) -> DispatchContext:
    """Build a fresh ``DispatchContext`` for a unique 32-hex-char trace id."""
    return DispatchContext(_instr(trace))


def _get(url: str = "https://example.com/start") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


def _stages_of(pipeline: Pipeline) -> list[Stage]:
    out: list[Stage] = []
    node: Policy | None = pipeline._chain
    while node is not None and not isinstance(node, _TransportRunner):
        out.append(node.STAGE)
        node = getattr(node, "next", None)
    return out


def _async_stages_of(pipeline: AsyncPipeline) -> list[Stage]:
    out: list[Stage] = []
    node: AsyncPolicy | None = pipeline._chain
    while node is not None and not isinstance(node, _AsyncTransportRunner):
        out.append(node.STAGE)
        node = getattr(node, "next", None)
    return out


def _policies_of(pipeline: Pipeline) -> list[type[Policy]]:
    out: list[type[Policy]] = []
    node: Policy | None = pipeline._chain
    while node is not None and not isinstance(node, _TransportRunner):
        out.append(type(node))
        node = getattr(node, "next", None)
    return out


def _async_policies_of(pipeline: AsyncPipeline) -> list[type[AsyncPolicy]]:
    out: list[type[AsyncPolicy]] = []
    node: AsyncPolicy | None = pipeline._chain
    while node is not None and not isinstance(node, _AsyncTransportRunner):
        out.append(type(node))
        node = getattr(node, "next", None)
    return out


def test_default_pipeline_returns_builder() -> None:
    builder = default_pipeline(_StubTransport())
    assert isinstance(builder, StagedPipelineBuilder)


def test_default_pipeline_wires_canonical_stack() -> None:
    pipeline = default_pipeline(_StubTransport()).build()
    stages = _stages_of(pipeline)
    # Canonical order: OPERATION (operation-tracing, outside the wrappers),
    # REDIRECT, POST_REDIRECT (idempotency), RETRY, POST_RETRY (set-date then
    # client-identity), LOGGING, POST_LOGGING (tracing).
    assert stages == [
        Stage.OPERATION,
        Stage.REDIRECT,
        Stage.POST_REDIRECT,
        Stage.RETRY,
        Stage.POST_RETRY,
        Stage.POST_RETRY,
        Stage.LOGGING,
        Stage.POST_LOGGING,
    ]


def test_default_pipeline_exact_policy_order() -> None:
    # Introspect the concrete policy types (stronger than stage-only): the
    # fixed order is operation-tracing -> redirect -> idempotency -> retry ->
    # set-date -> client-identity -> logging -> tracing -> transport.
    pipeline = default_pipeline(_StubTransport()).build()
    assert _policies_of(pipeline) == [
        OperationTracingPolicy,
        RedirectPolicy,
        IdempotencyPolicy,
        RetryPolicy,
        SetDatePolicy,
        ClientIdentityPolicy,
        LoggingPolicy,
        TracingPolicy,
    ]


def test_default_pipeline_with_auth_exact_policy_order() -> None:
    # Auth slots between client-identity and logging — i.e. INSIDE the retry
    # wrapper (Stage.AUTH > Stage.RETRY), so a 401 refresh replays the request.
    auth = KeyCredentialPolicy(KeyCredential("secret"), "X-API-Key")
    pipeline = default_pipeline(_StubTransport(), auth=auth).build()
    assert _policies_of(pipeline) == [
        OperationTracingPolicy,
        RedirectPolicy,
        IdempotencyPolicy,
        RetryPolicy,
        SetDatePolicy,
        ClientIdentityPolicy,
        KeyCredentialPolicy,
        LoggingPolicy,
        TracingPolicy,
    ]


def test_default_pipeline_idempotency_outside_retry_auth_inside() -> None:
    # Idempotency wraps retry so a write's key is minted once and stays stable
    # across attempts; auth sits inside retry so a 401 refresh can replay.
    assert IdempotencyPolicy.STAGE < RetryPolicy.STAGE
    assert KeyCredentialPolicy.STAGE > RetryPolicy.STAGE


def test_default_pipeline_with_auth_inserts_at_AUTH_stage() -> None:  # noqa: N802
    auth = KeyCredentialPolicy(KeyCredential("secret"), "X-API-Key")
    pipeline = default_pipeline(_StubTransport(), auth=auth).build()
    stages = _stages_of(pipeline)
    assert Stage.AUTH in stages
    # Auth slots between POST_RETRY and LOGGING
    assert stages.index(Stage.AUTH) < stages.index(Stage.LOGGING)


def test_default_pipeline_override_replaces_default() -> None:
    custom_retry = RetryPolicy(total_retries=42)
    builder = default_pipeline(_StubTransport(), retry=custom_retry)
    # The builder's pillar for RETRY should be the custom instance
    assert builder._pillars[Stage.RETRY] is custom_retry


def test_default_pipeline_no_auth_by_default() -> None:
    pipeline = default_pipeline(_StubTransport()).build()
    assert Stage.AUTH not in _stages_of(pipeline)


def test_default_pipeline_explicit_overrides() -> None:
    redirect = RedirectPolicy(max_hops=2)
    retry = RetryPolicy(total_retries=0)
    set_date = SetDatePolicy()
    logging = LoggingPolicy()
    tracing = TracingPolicy()
    builder = default_pipeline(
        _StubTransport(),
        redirect=redirect,
        retry=retry,
        set_date=set_date,
        logging=logging,
        tracing=tracing,
    )
    assert builder._pillars[Stage.REDIRECT] is redirect
    assert builder._pillars[Stage.RETRY] is retry
    assert builder._pillars[Stage.LOGGING] is logging


def test_default_pipeline_builder_is_extensible() -> None:
    """The factory returns a builder, not a built pipeline — callers can keep editing."""
    builder = default_pipeline(_StubTransport())
    # Replacing the retry pillar via the surgical API should work
    new_retry = RetryPolicy(total_retries=99)
    builder.replace(RetryPolicy, new_retry)
    assert builder._pillars[Stage.RETRY] is new_retry


def test_default_async_pipeline_returns_builder() -> None:
    builder = default_async_pipeline(_AsyncStubTransport())
    assert isinstance(builder, AsyncStagedPipelineBuilder)


def test_default_async_pipeline_builds_async_pipeline() -> None:
    pipeline = default_async_pipeline(_AsyncStubTransport()).build()
    assert isinstance(pipeline, AsyncPipeline)


def test_default_async_pipeline_wires_operation_tracing() -> None:
    from dexpace.sdk.core.pipeline.policies import AsyncOperationTracingPolicy

    builder = default_async_pipeline(_AsyncStubTransport())
    # Mirrors the sync default: the per-operation lifecycle policy occupies the
    # outermost OPERATION pillar, so async callers get the same
    # operation_started / operation_succeeded / operation_failed bracket around
    # the attempt-level events the retry policy emits.
    assert isinstance(builder._pillars[Stage.OPERATION], AsyncOperationTracingPolicy)


def test_default_async_pipeline_exact_policy_order() -> None:
    # The async default omits redirect (opt-in only) — the shared resolution
    # core isn't yet certified against the full redirect security battery
    # (docs/deviations.md) — but DOES carry the async logging + per-attempt
    # tracing policies inside the retry wrapper, matching the sync default's
    # instrumentation slots. The exact-order assertion certifies both the
    # redirect omission and the instrumentation policies' positions.
    pipeline = default_async_pipeline(_AsyncStubTransport()).build()
    assert _async_policies_of(pipeline) == [
        AsyncOperationTracingPolicy,
        AsyncIdempotencyPolicy,
        AsyncRetryPolicy,
        AsyncSetDatePolicy,
        AsyncClientIdentityPolicy,
        AsyncLoggingPolicy,
        AsyncTracingPolicy,
    ]


def test_default_async_pipeline_opt_in_redirect_exact_policy_order() -> None:
    pipeline = default_async_pipeline(_AsyncStubTransport(), redirect=AsyncRedirectPolicy()).build()
    assert _async_policies_of(pipeline) == [
        AsyncOperationTracingPolicy,
        AsyncRedirectPolicy,
        AsyncIdempotencyPolicy,
        AsyncRetryPolicy,
        AsyncSetDatePolicy,
        AsyncClientIdentityPolicy,
        AsyncLoggingPolicy,
        AsyncTracingPolicy,
    ]


def test_default_async_pipeline_with_auth_exact_policy_order() -> None:
    pipeline = default_async_pipeline(_AsyncStubTransport(), auth=_StubAsyncAuth()).build()
    assert _async_policies_of(pipeline) == [
        AsyncOperationTracingPolicy,
        AsyncIdempotencyPolicy,
        AsyncRetryPolicy,
        AsyncSetDatePolicy,
        AsyncClientIdentityPolicy,
        _StubAsyncAuth,
        AsyncLoggingPolicy,
        AsyncTracingPolicy,
    ]


def test_default_async_pipeline_wires_logging_and_tracing() -> None:
    builder = default_async_pipeline(_AsyncStubTransport())
    # The async wire-logging gap is closed: the LOGGING pillar holds the async
    # logging policy and the per-attempt tracing span sits at POST_LOGGING,
    # matching the sync default's instrumentation slots.
    assert isinstance(builder._pillars[Stage.LOGGING], AsyncLoggingPolicy)
    stages = _async_stages_of(builder.build())
    assert Stage.LOGGING in stages
    assert Stage.POST_LOGGING in stages
    # Logging / tracing sit inside the retry wrapper (per attempt), not before it.
    assert stages.index(Stage.RETRY) < stages.index(Stage.LOGGING)
    assert stages.index(Stage.LOGGING) < stages.index(Stage.POST_LOGGING)


def test_default_async_pipeline_logging_tracing_overrides_replace_defaults() -> None:
    logging = AsyncLoggingPolicy(preview_bytes=256)
    tracing = AsyncTracingPolicy()
    builder = default_async_pipeline(_AsyncStubTransport(), logging=logging, tracing=tracing)
    assert builder._pillars[Stage.LOGGING] is logging


def test_default_async_pipeline_omits_redirect_by_default() -> None:
    # Gate: the async redirect twin is not certified against the full redirect
    # security battery (see test_async_redirect_battery.py and docs/deviations.md),
    # so the async default must not silently follow redirects. The REDIRECT
    # pillar stays empty unless the caller opts in.
    builder = default_async_pipeline(_AsyncStubTransport())
    assert Stage.REDIRECT not in builder._pillars
    assert Stage.REDIRECT not in _async_stages_of(builder.build())


def test_default_async_pipeline_opt_in_redirect_wires_at_redirect_stage() -> None:
    # Opt-in path stays fully functional: an explicit AsyncRedirectPolicy is
    # wired into the REDIRECT pillar exactly as the sync default wires its own.
    redirect = AsyncRedirectPolicy(max_hops=3)
    builder = default_async_pipeline(_AsyncStubTransport(), redirect=redirect)
    assert builder._pillars[Stage.REDIRECT] is redirect
    assert Stage.REDIRECT in _async_stages_of(builder.build())


def test_default_pipeline_options_identity_across_retry_and_redirect() -> None:
    # Force a retry (503) then a redirect (301) then success through the full
    # default stack. The per-call options mapping must be the same object at
    # the transport boundary on every fork, threaded through unchanged.
    seen: list[dict[str, Any]] = []
    client = _OwnershipTransport(
        [
            (Status.SERVICE_UNAVAILABLE, None),  # retry fork
            (Status.MOVED_PERMANENTLY, "https://example.com/new"),  # redirect fork
            (Status.OK, None),  # final
        ],
        owns=False,
    )
    builder = default_pipeline(client, retry=RetryPolicy(backoff_factor=0.0, clock=FakeClock()))
    builder.append(_OptionsProbe(seen))
    response = builder.build().run(_get(), _dispatch(f"{1:032x}"), retry_total=5, marker="abc")
    response.close()
    assert len(client.requests) == 3  # the retry + redirect walk actually ran
    assert len(seen) == 3  # one observation per fork reaching the transport
    assert all(observed is seen[0] for observed in seen)  # same mapping object every fork
    assert seen[0]["retry_total"] == 5  # caller override threaded through unchanged
    assert seen[0]["marker"] == "abc"
    assert client.close_calls == 0  # the run/fork machinery never closed the transport


async def test_default_async_pipeline_options_identity_across_retry() -> None:
    # The async default has no redirect to force, so exercise retry: a 503 then
    # success. The options mapping stays the same object across both attempts.
    seen: list[dict[str, Any]] = []
    client = _AsyncOwnershipTransport(
        [(Status.SERVICE_UNAVAILABLE, None), (Status.OK, None)],
        owns=False,
    )
    builder = default_async_pipeline(
        client, retry=AsyncRetryPolicy(backoff_factor=0.0, clock=_AsyncFakeClock())
    )
    builder.append(_AsyncOptionsProbe(seen))
    response = await builder.build().run(
        _get(), _dispatch(f"{2:032x}"), retry_total=5, marker="abc"
    )
    await response.close()
    assert len(client.requests) == 2
    assert len(seen) == 2
    assert all(observed is seen[0] for observed in seen)
    assert seen[0]["retry_total"] == 5
    assert seen[0]["marker"] == "abc"
    assert client.aclose_calls == 0


def test_default_pipeline_run_never_closes_transport() -> None:
    # Driven without the context manager, the run/fork machinery never closes
    # the transport — the caller retains ownership throughout normal operation.
    client = _OwnershipTransport(owns=True)
    pipeline = default_pipeline(client).build()
    pipeline.run(_get(), _dispatch(f"{3:032x}")).close()
    assert client.close_calls == 0
    assert client.underlying_torn_down is False


def test_default_pipeline_byo_transport_survives_close() -> None:
    # A borrowed (BYO) transport: the deliberate context-manager exit delegates
    # to transport.close(), but an ownership-aware close leaves the caller's
    # underlying resource intact.
    client = _OwnershipTransport(owns=False)
    with default_pipeline(client).build() as pipeline:
        pipeline.run(_get(), _dispatch(f"{4:032x}")).close()
    assert client.close_calls == 1  # close was delegated to the transport
    assert client.underlying_torn_down is False  # ... but the BYO resource survives


def test_default_pipeline_owned_transport_closed_on_exit() -> None:
    # The other side of the ownership seam: a transport that owns its underlying
    # resource does tear it down on the deliberate context-manager exit.
    client = _OwnershipTransport(owns=True)
    with default_pipeline(client).build() as pipeline:
        pipeline.run(_get(), _dispatch(f"{5:032x}")).close()
    assert client.underlying_torn_down is True


def test_default_pipeline_close_is_idempotent() -> None:
    # Double close is a no-op: a second context-manager exit re-invokes
    # transport.close(), but the idempotency guard means teardown never doubles.
    client = _OwnershipTransport(owns=True)
    pipeline = default_pipeline(client).build()
    with pipeline:
        pipeline.run(_get(), _dispatch(f"{6:032x}")).close()
    with pipeline:  # second deliberate exit -> second close()
        pass
    assert client.close_calls == 2  # the pipeline delegated the close twice
    assert client.underlying_torn_down is True  # ... teardown happened exactly once


async def test_default_async_pipeline_run_never_closes_transport() -> None:
    client = _AsyncOwnershipTransport(owns=True)
    pipeline = default_async_pipeline(client).build()
    response = await pipeline.run(_get(), _dispatch(f"{7:032x}"))
    await response.close()
    assert client.aclose_calls == 0
    assert client.underlying_torn_down is False


async def test_default_async_pipeline_byo_transport_survives_close() -> None:
    client = _AsyncOwnershipTransport(owns=False)
    async with default_async_pipeline(client).build() as pipeline:
        response = await pipeline.run(_get(), _dispatch(f"{8:032x}"))
        await response.close()
    assert client.aclose_calls == 1
    assert client.underlying_torn_down is False


async def test_default_async_pipeline_close_is_idempotent() -> None:
    client = _AsyncOwnershipTransport(owns=True)
    pipeline = default_async_pipeline(client).build()
    async with pipeline:
        response = await pipeline.run(_get(), _dispatch(f"{9:032x}"))
        await response.close()
    async with pipeline:  # second deliberate exit -> second aclose()
        pass
    assert client.aclose_calls == 2
    assert client.underlying_torn_down is True
