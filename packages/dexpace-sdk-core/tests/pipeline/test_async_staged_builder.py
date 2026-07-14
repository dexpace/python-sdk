# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncStagedPipelineBuilder``.

Trimmed test surface — exercises the async-specific differences (AsyncPolicy
subclassing, AsyncPipeline output). Behavioural parity with the sync builder
is covered by ``test_staged_builder.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dexpace.sdk.core.pipeline import AsyncPipeline, AsyncStagedPipelineBuilder, Stage
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy

if TYPE_CHECKING:
    from dexpace.sdk.core.http.context.call_context import CallContext
    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.http.response.async_response import AsyncResponse
    from dexpace.sdk.core.pipeline.context import PipelineContext


class _AsyncStubTransport:
    async def execute(self, request: Request) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


class _AsyncMarkerPolicy(AsyncPolicy):
    STAGE = Stage.PRE_AUTH
    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return await self.next.send(request, ctx)


class _AsyncLoggingPillar(AsyncPolicy):
    """Second pillar (LOGGING) for mutate-while-iterating regression."""

    STAGE = Stage.LOGGING
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return await self.next.send(request, ctx)


class _AsyncSecondMarkerPolicy(AsyncPolicy):
    """Non-pillar policy at POST_AUTH for batch/stacking tests."""

    STAGE = Stage.POST_AUTH
    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return await self.next.send(request, ctx)


@pytest.fixture
def async_transport() -> _AsyncStubTransport:
    return _AsyncStubTransport()


def test_build_orders_by_stage(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    b.append(_AsyncMarkerPolicy("pre")).append(AsyncRetryPolicy())
    pipeline = b.build()
    assert isinstance(pipeline, AsyncPipeline)


@pytest.mark.req("PIPE-28")
def test_pillar_double_append_raises(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    b.append(AsyncRetryPolicy())
    with pytest.raises(ValueError, match="Pillar stage RETRY"):
        b.append(AsyncRetryPolicy())


def test_pillar_force_overwrites(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    first = AsyncRetryPolicy()
    second = AsyncRetryPolicy()
    b.append(first)
    b.append(second, force=True)
    assert b._pillars[Stage.RETRY] is second


def test_replace_pillar(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    first = AsyncRetryPolicy()
    second = AsyncRetryPolicy()
    b.append(first).replace(AsyncRetryPolicy, second)
    assert b._pillars[Stage.RETRY] is second


def test_replace_pillar_with_multiple_pillars_present(
    async_transport: _AsyncStubTransport,
) -> None:
    # Regression: ``replace`` used to ``del`` from ``_pillars`` while
    # iterating it. With a second pillar present the lookup must finish
    # before the deletion, leaving the other pillar untouched.
    b = AsyncStagedPipelineBuilder(async_transport)
    logging = _AsyncLoggingPillar()
    b.append(AsyncRetryPolicy()).append(logging)
    new_retry = AsyncRetryPolicy()
    b.replace(AsyncRetryPolicy, new_retry)
    assert b._pillars[Stage.RETRY] is new_retry
    assert b._pillars[Stage.LOGGING] is logging


def test_from_pipeline_round_trip(async_transport: _AsyncStubTransport) -> None:
    original = AsyncPipeline(
        async_transport,
        policies=[AsyncRetryPolicy()],
    )
    rebuilt = AsyncStagedPipelineBuilder.from_pipeline(original).build()
    assert isinstance(rebuilt, AsyncPipeline)


def test_from_pipeline_sansio_step_raises_value_error(
    async_transport: _AsyncStubTransport,
) -> None:
    # A bare callable becomes an internal SansIO runner with no STAGE.
    # from_pipeline must surface a clear ValueError, not a raw AttributeError.
    def stamp(request: Request, ctx: CallContext) -> Request:
        return request

    original = AsyncPipeline(async_transport, policies=[stamp])
    with pytest.raises(ValueError, match="SansIO"):
        AsyncStagedPipelineBuilder.from_pipeline(original)


def test_insert_second_pillar_via_splice_raises(
    async_transport: _AsyncStubTransport,
) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    original = AsyncRetryPolicy()
    b.append(original).append(_AsyncMarkerPolicy("m"))
    # ``insert_after`` re-buckets through ``_reload``; a second retry pillar
    # must not silently clobber the incumbent.
    with pytest.raises(ValueError, match="RETRY"):
        b.insert_after(_AsyncMarkerPolicy, AsyncRetryPolicy())
    assert b._pillars[Stage.RETRY] is original
    assert [p.tag for p in b._flatten() if isinstance(p, _AsyncMarkerPolicy)] == ["m"]


@pytest.mark.req("PIPE-28")
def test_replace_pillar_with_different_stage_rejected(
    async_transport: _AsyncStubTransport,
) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    retry = AsyncRetryPolicy()
    b.append(retry)
    with pytest.raises(ValueError, match="relocate"):
        b.replace(AsyncRetryPolicy, _AsyncMarkerPolicy("x"))
    assert b._pillars[Stage.RETRY] is retry
    assert not any(isinstance(p, _AsyncMarkerPolicy) for p in b._flatten())


def test_replace_cross_stage_conflict_is_atomic(
    async_transport: _AsyncStubTransport,
) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    b.append(AsyncRetryPolicy()).append(_AsyncMarkerPolicy("m"))
    with pytest.raises(ValueError, match="RETRY"):
        b.replace(_AsyncMarkerPolicy, AsyncRetryPolicy())
    assert [p.tag for p in b._flatten() if isinstance(p, _AsyncMarkerPolicy)] == ["m"]


def test_batch_rolls_back_on_failure(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    original = AsyncRetryPolicy()
    b.append(original)
    with pytest.raises(ValueError, match="RETRY"), b.batch() as bb:
        bb.append(_AsyncMarkerPolicy("m1"))
        bb.append(_AsyncSecondMarkerPolicy("m2"))
        bb.append(AsyncRetryPolicy())  # duplicate pillar → raises
    flat = b._flatten()
    assert b._pillars[Stage.RETRY] is original
    assert not any(isinstance(p, _AsyncMarkerPolicy) for p in flat)
    assert not any(isinstance(p, _AsyncSecondMarkerPolicy) for p in flat)


def test_batch_commits_on_success(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    with b.batch() as bb:
        bb.append(_AsyncMarkerPolicy("m1")).append(_AsyncSecondMarkerPolicy("m2"))
    flat = b._flatten()
    assert any(isinstance(p, _AsyncMarkerPolicy) for p in flat)
    assert any(isinstance(p, _AsyncSecondMarkerPolicy) for p in flat)
