"""Tests for ``default_pipeline`` and ``default_async_pipeline`` factories."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dexpace.sdk.core.http.auth.credentials import KeyCredential
from dexpace.sdk.core.http.auth.policies import KeyCredentialPolicy
from dexpace.sdk.core.pipeline import Pipeline, Stage, StagedPipelineBuilder
from dexpace.sdk.core.pipeline._transport_runner import _TransportRunner
from dexpace.sdk.core.pipeline.async_pipeline import AsyncPipeline
from dexpace.sdk.core.pipeline.async_staged_builder import AsyncStagedPipelineBuilder
from dexpace.sdk.core.pipeline.defaults import default_async_pipeline, default_pipeline
from dexpace.sdk.core.pipeline.policies.logging_policy import LoggingPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy
from dexpace.sdk.core.pipeline.policies.set_date import SetDatePolicy
from dexpace.sdk.core.pipeline.policies.tracing_policy import TracingPolicy

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.http.response.async_response import AsyncResponse
    from dexpace.sdk.core.http.response.response import Response
    from dexpace.sdk.core.pipeline.policy import Policy


class _StubTransport:
    def execute(self, request: Request) -> Response:  # pragma: no cover
        raise NotImplementedError


class _AsyncStubTransport:
    async def execute(self, request: Request) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


def _stages_of(pipeline: Pipeline) -> list[Stage]:
    out: list[Stage] = []
    node: Policy | None = pipeline._chain
    while node is not None and not isinstance(node, _TransportRunner):
        out.append(node.STAGE)
        node = getattr(node, "next", None)
    return out


def test_default_pipeline_returns_builder() -> None:
    builder = default_pipeline(_StubTransport())
    assert isinstance(builder, StagedPipelineBuilder)


def test_default_pipeline_wires_canonical_stack() -> None:
    pipeline = default_pipeline(_StubTransport()).build()
    stages = _stages_of(pipeline)
    # Canonical order: REDIRECT, RETRY, POST_RETRY (set-date), LOGGING, POST_LOGGING
    assert stages == [
        Stage.REDIRECT,
        Stage.RETRY,
        Stage.POST_RETRY,
        Stage.LOGGING,
        Stage.POST_LOGGING,
    ]


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
