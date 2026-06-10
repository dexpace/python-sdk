# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``StagedPipelineBuilder``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from dexpace.sdk.core.pipeline import Pipeline, Policy, Stage, StagedPipelineBuilder
from dexpace.sdk.core.pipeline.policies.logging_policy import LoggingPolicy
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy
from dexpace.sdk.core.pipeline.policies.tracing_policy import TracingPolicy

if TYPE_CHECKING:
    from dexpace.sdk.core.http.context.call_context import CallContext
    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.http.response.response import Response
    from dexpace.sdk.core.pipeline.context import PipelineContext


class _StubTransport:
    """Minimal HttpClient that returns a fixed Response."""

    def execute(self, request: Request) -> Response:  # pragma: no cover — never called here
        raise NotImplementedError


class _MarkerPolicy(Policy):
    """Non-pillar policy at PRE_AUTH for stacking tests."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        return self.next.send(request, ctx)


class _SecondMarkerPolicy(Policy):
    """Non-pillar policy at POST_AUTH for cross-stage tests."""

    STAGE = Stage.POST_AUTH
    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        return self.next.send(request, ctx)


@pytest.fixture
def transport() -> _StubTransport:
    return _StubTransport()


class TestAppendPrepend:
    def test_append_non_pillar_stacks(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(_MarkerPolicy("a")).append(_MarkerPolicy("b"))
        flat = b._flatten()
        assert [p.tag for p in flat if isinstance(p, _MarkerPolicy)] == ["a", "b"]

    def test_prepend_non_pillar_inserts_at_head(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(_MarkerPolicy("a")).prepend(_MarkerPolicy("z"))
        flat = b._flatten()
        assert [p.tag for p in flat if isinstance(p, _MarkerPolicy)] == ["z", "a"]

    def test_append_pillar_slots_once(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        retry = RetryPolicy()
        b.append(retry)
        assert b._pillars[Stage.RETRY] is retry

    def test_append_pillar_second_time_raises(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(RetryPolicy())
        with pytest.raises(ValueError, match="Pillar stage RETRY is already filled"):
            b.append(RetryPolicy())

    def test_append_pillar_with_force_overwrites(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        first = RetryPolicy()
        replacement = RetryPolicy()
        b.append(first)
        b.append(replacement, force=True)
        assert b._pillars[Stage.RETRY] is replacement


class TestSurgicalEdits:
    def test_replace_pillar(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        original = RetryPolicy()
        new = RetryPolicy()
        b.append(original).replace(RetryPolicy, new)
        assert b._pillars[Stage.RETRY] is new

    def test_replace_non_pillar(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(_MarkerPolicy("a")).append(_MarkerPolicy("b"))
        new = _MarkerPolicy("replacement")
        b.replace(_MarkerPolicy, new)
        flat = b._flatten()
        assert any(p is new for p in flat)

    def test_replace_missing_raises(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        with pytest.raises(ValueError, match="No instance of RetryPolicy"):
            b.replace(RetryPolicy, RetryPolicy())

    def test_insert_after(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        a = _MarkerPolicy("a")
        b_pol = _MarkerPolicy("b")
        b.append(a).append(b_pol)
        inserted = _MarkerPolicy("inserted")
        b.insert_after(_MarkerPolicy, inserted)
        tags = [p.tag for p in b._flatten() if isinstance(p, _MarkerPolicy)]
        # insert_after first match (a) → inserted slots between a and b
        assert tags == ["a", "inserted", "b"]

    def test_insert_before(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        a = _MarkerPolicy("a")
        b_pol = _MarkerPolicy("b")
        b.append(a).append(b_pol)
        inserted = _MarkerPolicy("inserted")
        b.insert_before(_MarkerPolicy, inserted)
        tags = [p.tag for p in b._flatten() if isinstance(p, _MarkerPolicy)]
        assert tags == ["inserted", "a", "b"]

    def test_remove_all_of_type(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(_MarkerPolicy("a")).append(_MarkerPolicy("b")).append(_SecondMarkerPolicy("c"))
        b.remove(_MarkerPolicy)
        flat = b._flatten()
        assert not any(isinstance(p, _MarkerPolicy) for p in flat)
        assert any(isinstance(p, _SecondMarkerPolicy) for p in flat)


class TestBuild:
    def test_build_orders_by_stage(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        # Append in scrambled order
        b.append(LoggingPolicy()).append(RetryPolicy()).append(_MarkerPolicy("auth-side"))
        pipeline = b.build()
        # Walk the chain
        from dexpace.sdk.core.pipeline._transport_runner import _TransportRunner

        stages: list[Stage] = []
        node: Policy | None = pipeline._chain
        while node is not None and not isinstance(node, _TransportRunner):
            stages.append(node.STAGE)
            node = getattr(node, "next", None)
        # Expected non-decreasing: RETRY, PRE_AUTH, LOGGING
        assert stages == [Stage.RETRY, Stage.PRE_AUTH, Stage.LOGGING]

    def test_build_empty(self, transport: _StubTransport) -> None:
        b = StagedPipelineBuilder(transport)
        pipeline = b.build()
        assert isinstance(pipeline, Pipeline)


class TestFromPipeline:
    def test_round_trip(self, transport: _StubTransport) -> None:
        original = Pipeline(
            transport,
            policies=[RetryPolicy(), LoggingPolicy(), TracingPolicy()],
        )
        b = StagedPipelineBuilder.from_pipeline(original)
        rebuilt = b.build()
        # The rebuilt pipeline should have the same stage ordering
        from dexpace.sdk.core.pipeline._transport_runner import _TransportRunner

        def stages(p: Pipeline) -> list[Stage]:
            out: list[Stage] = []
            node: Policy | None = p._chain
            while node is not None and not isinstance(node, _TransportRunner):
                out.append(node.STAGE)
                node = getattr(node, "next", None)
            return out

        assert stages(rebuilt) == stages(original)

    def test_misordered_pipeline_raises(self, transport: _StubTransport) -> None:
        # Build a misordered pipeline: LOGGING before RETRY
        original = Pipeline(
            transport,
            policies=[LoggingPolicy(), RetryPolicy()],
        )
        with pytest.raises(ValueError, match="non-decreasing stage order"):
            StagedPipelineBuilder.from_pipeline(original)

    def test_sansio_step_raises_value_error(self, transport: _StubTransport) -> None:
        # A bare callable becomes an internal SansIO runner with no STAGE.
        # from_pipeline must surface a clear ValueError, not a raw AttributeError.
        def stamp(request: Request, ctx: CallContext) -> Request:
            return request

        original = Pipeline(transport, policies=[stamp])
        with pytest.raises(ValueError, match="SansIO"):
            StagedPipelineBuilder.from_pipeline(original)


class TestPillarReplacementSafety:
    """Default raises; force=True is the explicit escape."""

    def test_double_pillar_without_force_surfaces_loud_error(
        self,
        transport: _StubTransport,
    ) -> None:
        b = StagedPipelineBuilder(transport)
        b.append(RetryPolicy())
        with pytest.raises(ValueError) as exc_info:
            b.append(RetryPolicy())
        # Error message guides the user toward the explicit escape
        assert "force=True" in str(exc_info.value) or "replace(" in str(exc_info.value)

    def test_replace_method_is_the_recommended_swap(
        self,
        transport: _StubTransport,
    ) -> None:
        b = StagedPipelineBuilder(transport)
        first = RetryPolicy()
        second = RetryPolicy()
        b.append(first)
        # No force required for the explicit replace path
        b.replace(RetryPolicy, second)
        assert b._pillars[Stage.RETRY] is second


def _unused(_: Any) -> None: ...
