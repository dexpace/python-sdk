# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Single-pipeline ownership of ``Policy`` / ``AsyncPolicy`` instances.

A policy's ``.next`` is wired in place by the pipeline constructor. Sharing
one instance between two pipelines used to silently re-point the first
pipeline's chain at the second's transport. These tests pin the guard that
now rejects that reuse, for both the sync and async pipelines, and confirm
the ``StagedPipelineBuilder.from_pipeline`` round-trip still rebuilds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
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
    AsyncPolicy,
    AsyncStagedPipelineBuilder,
    Pipeline,
    Policy,
    Stage,
    StagedPipelineBuilder,
)

if TYPE_CHECKING:
    from dexpace.sdk.core.pipeline.context import PipelineContext


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


class _StubClient(HttpClient):
    def __init__(self) -> None:
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _StubAsyncClient(AsyncHttpClient):
    def __init__(self) -> None:
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls.append(request)
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _MarkerPolicy(Policy):
    STAGE = Stage.PRE_AUTH
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        return self.next.send(request, ctx)


class _AsyncMarkerPolicy(AsyncPolicy):
    STAGE = Stage.PRE_AUTH
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        return await self.next.send(request, ctx)


class TestSyncReuse:
    def test_sharing_policy_between_pipelines_raises(self) -> None:
        shared = _MarkerPolicy()
        Pipeline(_StubClient(), policies=[shared])
        # Building a second pipeline with the same instance would re-point
        # the first pipeline's chain — the guard refuses it.
        with pytest.raises(ValueError, match="already wired into another pipeline"):
            Pipeline(_StubClient(), policies=[shared])

    def test_first_pipeline_chain_untouched_after_rejected_reuse(self) -> None:
        client_a = _StubClient()
        shared = _MarkerPolicy()
        pipeline_a = Pipeline(client_a, policies=[shared])
        original_next = shared.next
        with pytest.raises(ValueError):
            Pipeline(_StubClient(), policies=[shared])
        # The rejected construction left ``shared`` pointing into pipeline A.
        assert shared.next is original_next
        with pipeline_a as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
        assert len(client_a.calls) == 1

    def test_distinct_instances_build_independently(self) -> None:
        client_a, client_b = _StubClient(), _StubClient()
        with Pipeline(client_a, policies=[_MarkerPolicy()]) as a:
            a.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
        with Pipeline(client_b, policies=[_MarkerPolicy()]) as b:
            b.run(_request(), DispatchContext(_instr("0" * 16 + "3")))
        assert len(client_a.calls) == 1
        assert len(client_b.calls) == 1

    def test_from_pipeline_rebuilds_with_reused_instances(self) -> None:
        # from_pipeline detaches the harvested policies, so build() re-wires
        # the same instances without tripping the single-ownership guard.
        client = _StubClient()
        marker = _MarkerPolicy()
        original = Pipeline(client, policies=[marker])
        rebuilt = StagedPipelineBuilder.from_pipeline(original).build()
        assert list(_walk(rebuilt)) == [marker]
        with rebuilt as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
        assert len(client.calls) == 1

    def test_from_pipeline_detaches_source_and_reuse_routes_to_new_transport(self) -> None:
        # M17 motivating case: "build a default, harvest via from_pipeline,
        # tweak". Harvesting consumes the source (its policies are detached),
        # so the same instance re-homes on a DIFFERENT transport and routes
        # only there — never leaking back to the original transport.
        client_a, client_b = _StubClient(), _StubClient()
        marker = _MarkerPolicy()
        original = Pipeline(client_a, policies=[marker])
        StagedPipelineBuilder.from_pipeline(original)
        assert getattr(marker, "next", None) is None  # detached from pipeline A
        rebuilt = Pipeline(client_b, policies=[marker])
        with rebuilt as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "5")))
        assert len(client_b.calls) == 1  # routed to the new transport only
        assert len(client_a.calls) == 0  # never leaked back to the old transport


class TestAsyncReuse:
    def test_sharing_async_policy_between_pipelines_raises(self) -> None:
        shared = _AsyncMarkerPolicy()
        AsyncPipeline(_StubAsyncClient(), policies=[shared])
        with pytest.raises(ValueError, match="already wired into another pipeline"):
            AsyncPipeline(_StubAsyncClient(), policies=[shared])

    def test_async_first_pipeline_chain_untouched_after_rejected_reuse(self) -> None:
        shared = _AsyncMarkerPolicy()
        AsyncPipeline(_StubAsyncClient(), policies=[shared])
        original_next = shared.next
        with pytest.raises(ValueError):
            AsyncPipeline(_StubAsyncClient(), policies=[shared])
        assert shared.next is original_next

    def test_async_from_pipeline_rebuilds_with_reused_instances(self) -> None:
        original = AsyncPipeline(_StubAsyncClient(), policies=[_AsyncMarkerPolicy()])
        # Detaching during harvest lets the rebuild re-wire without error.
        rebuilt = AsyncStagedPipelineBuilder.from_pipeline(original).build()
        assert isinstance(rebuilt, AsyncPipeline)

    def test_async_from_pipeline_detaches_source_enabling_distinct_transport(self) -> None:
        # M17 async twin: harvest detaches the source policy, so it can be
        # re-homed on a different transport without tripping the guard.
        marker = _AsyncMarkerPolicy()
        original = AsyncPipeline(_StubAsyncClient(), policies=[marker])
        AsyncStagedPipelineBuilder.from_pipeline(original)
        assert getattr(marker, "next", None) is None  # detached from the source
        AsyncPipeline(_StubAsyncClient(), policies=[marker])  # re-home: no guard trip


class TestDocstringMatchesBehaviour:
    """L11: the docstring described a phantom decorator/tagging mechanism."""

    def test_pipeline_docstring_drops_phantom_feature(self) -> None:
        doc = Pipeline.__doc__ or ""
        # The real mechanism is the optional ``side`` attribute.
        assert "side" in doc
        # The phantom prose is gone.
        assert "request_side" not in doc
        assert "response_side" not in doc
        assert "step decorators" not in doc

    def test_async_pipeline_docstring_drops_phantom_feature(self) -> None:
        doc = AsyncPipeline.__doc__ or ""
        assert "side" in doc
        assert "request_side" not in doc
        assert "response_side" not in doc
        assert "step decorators" not in doc


def _walk(pipeline: Pipeline) -> list[Policy]:
    from dexpace.sdk.core.pipeline._transport_runner import _TransportRunner

    out: list[Policy] = []
    node: Policy | None = pipeline._chain
    while node is not None and not isinstance(node, _TransportRunner):
        out.append(node)
        node = getattr(node, "next", None)
    return out
