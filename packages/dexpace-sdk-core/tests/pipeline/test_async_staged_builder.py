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


@pytest.fixture
def async_transport() -> _AsyncStubTransport:
    return _AsyncStubTransport()


def test_build_orders_by_stage(async_transport: _AsyncStubTransport) -> None:
    b = AsyncStagedPipelineBuilder(async_transport)
    b.append(_AsyncMarkerPolicy("pre")).append(AsyncRetryPolicy())
    pipeline = b.build()
    assert isinstance(pipeline, AsyncPipeline)


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
