# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Integration tests for `merge_config` / `merge_async_config`.

Covers the Tier-2 acceptance bar for PY-M9-04: exactly one pipeline is built
(never two nested), a user override wins field-wise over the service
defaults, the service's User-Agent token prepends to the platform/runtime
tokens via the existing `ClientIdentityPolicy` mechanism, an api-version
header survives a forced redirect + retry walk (re-stamped per hop/attempt),
and a BYO transport/pipeline is never closed by the executor built around it.
"""

from __future__ import annotations

from typing import Any

import pytest

from dexpace.sdk.core.codegen import (
    AsyncServiceCore,
    ServiceCore,
    ServiceDefaults,
    merge_async_config,
    merge_config,
)
from dexpace.sdk.core.codegen.status_error_map import StatusErrorMap
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy

from ..conftest import FakeClock
from .support import AsyncScriptTransport, Reply, ScriptTransport

_BASE = "https://api.example.com"


class _AsyncFakeClock:
    """Deterministic async clock; ``sleep`` advances simulated time, not real time."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)


def _get(url: str = _BASE) -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


# --------------------------------------------------------------------------- #
# flatten: exactly one pipeline built, user config wins field-wise              #
# --------------------------------------------------------------------------- #


def test_merge_config_builds_exactly_one_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    import dexpace.sdk.core.pipeline.pipeline as pipeline_module

    calls: list[object] = []
    original_init = pipeline_module.Pipeline.__init__

    def _spy(self: pipeline_module.Pipeline, *a: Any, **kw: Any) -> None:
        calls.append(self)
        original_init(self, *a, **kw)

    monkeypatch.setattr(pipeline_module.Pipeline, "__init__", _spy)

    transport = ScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="svc/1.0")
    result = merge_config(defaults, transport=transport)
    assert len(calls) == 1  # merge_config's own default_pipeline(...).build() call

    with ServiceCore(**result):
        pass
    assert len(calls) == 1  # ServiceCore(pipeline=...) borrows it; no second pipeline built


async def test_merge_async_config_builds_exactly_one_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dexpace.sdk.core.pipeline.async_pipeline as async_pipeline_module

    calls: list[object] = []
    original_init = async_pipeline_module.AsyncPipeline.__init__

    def _spy(self: async_pipeline_module.AsyncPipeline, *a: Any, **kw: Any) -> None:
        calls.append(self)
        original_init(self, *a, **kw)

    monkeypatch.setattr(async_pipeline_module.AsyncPipeline, "__init__", _spy)

    transport = AsyncScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="svc/1.0")
    result = merge_async_config(defaults, transport=transport)
    assert len(calls) == 1

    async with AsyncServiceCore(**result):
        pass
    assert len(calls) == 1


def test_merge_config_user_config_wins_field_wise() -> None:
    svc_errors = StatusErrorMap()
    user_errors = StatusErrorMap()
    defaults = ServiceDefaults(
        base_url="https://service-default.example.com",
        user_agent_token="svc/1.0",
        errors=svc_errors,
    )
    user = ServiceDefaults(base_url="https://user-override.example.com", errors=user_errors)
    transport = ScriptTransport([Reply(Status.OK)])
    result = merge_config(defaults, user, transport=transport)
    assert result["base_url"] == "https://user-override.example.com"  # user wins
    assert result["errors"] is user_errors  # user wins
    assert isinstance(result["pipeline"], Pipeline)


def test_merge_config_requires_exactly_one_of_transport_or_pipeline() -> None:
    defaults = ServiceDefaults(base_url=_BASE)
    with pytest.raises(ValueError, match="exactly one"):
        merge_config(defaults)
    with pytest.raises(ValueError, match="exactly one"):
        merge_config(
            defaults,
            transport=ScriptTransport([Reply(Status.OK)]),
            pipeline=Pipeline(ScriptTransport([Reply(Status.OK)])),
        )


def test_merge_config_requires_a_resolved_base_url() -> None:
    defaults = ServiceDefaults()
    with pytest.raises(ValueError, match="base_url"):
        merge_config(defaults, transport=ScriptTransport([Reply(Status.OK)]))


# --------------------------------------------------------------------------- #
# UA token ordering: service token prepends to platform/runtime                #
# --------------------------------------------------------------------------- #


def test_service_user_agent_token_prepends_to_platform_and_runtime() -> None:
    transport = ScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="acme-service/2.3")
    result = merge_config(defaults, transport=transport)
    with ServiceCore(**result) as core:
        core.execute_request(_get()).close()
    ua = transport.requests[0].headers.get("User-Agent")
    assert ua is not None
    service, platform, runtime = ua.split(" ")
    assert service == "acme-service/2.3"
    assert platform.startswith("dexpace-sdk/")
    assert runtime.startswith("python/")


def test_user_override_user_agent_token_wins_over_service_default() -> None:
    transport = ScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="svc-default/1.0")
    user = ServiceDefaults(user_agent_token="caller-override/9.9")
    result = merge_config(defaults, user, transport=transport)
    with ServiceCore(**result) as core:
        core.execute_request(_get()).close()
    ua = transport.requests[0].headers.get("User-Agent")
    assert ua is not None
    assert ua.startswith("caller-override/9.9 ")


async def test_async_service_user_agent_token_prepends_to_platform_and_runtime() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="acme-async-service/4.5")
    result = merge_async_config(defaults, transport=transport)
    async with AsyncServiceCore(**result) as core:
        response = await core.execute_request(_get())
        await response.close()
    ua = transport.requests[0].headers.get("User-Agent")
    assert ua is not None
    service, platform, runtime = ua.split(" ")
    assert service == "acme-async-service/4.5"
    assert platform.startswith("dexpace-sdk/")
    assert runtime.startswith("python/")


# --------------------------------------------------------------------------- #
# api-version header survives a forced redirect + retry walk                   #
# --------------------------------------------------------------------------- #


def test_api_version_header_restamped_on_every_hop_and_attempt() -> None:
    # Force a retry (503) then a redirect (301) then success, mirroring the
    # pattern in tests/pipeline/test_defaults.py. The api-version header must
    # land on every one of the three actual sends, not just the first.
    transport = ScriptTransport(
        [
            Reply(Status.SERVICE_UNAVAILABLE),
            Reply(Status.MOVED_PERMANENTLY, headers=(("Location", f"{_BASE}/new"),)),
            Reply(Status.OK),
        ]
    )
    defaults = ServiceDefaults(base_url=_BASE, api_version="2026-01-01")
    result = merge_config(
        defaults,
        transport=transport,
        retry=RetryPolicy(backoff_factor=0.0, clock=FakeClock()),
    )
    with ServiceCore(**result) as core:
        core.execute_request(_get()).close()
    assert len(transport.requests) == 3  # the retry + redirect walk actually ran
    assert all(req.headers.get("Api-Version") == "2026-01-01" for req in transport.requests)


def test_api_version_header_uses_configured_header_name() -> None:
    transport = ScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(
        base_url=_BASE, api_version="v2", api_version_header="X-Service-Api-Version"
    )
    result = merge_config(defaults, transport=transport)
    with ServiceCore(**result) as core:
        core.execute_request(_get()).close()
    assert transport.requests[0].headers.get("X-Service-Api-Version") == "v2"
    assert transport.requests[0].headers.get("Api-Version") is None


async def test_async_api_version_header_restamped_on_every_hop_and_attempt() -> None:
    transport = AsyncScriptTransport(
        [
            Reply(Status.SERVICE_UNAVAILABLE),
            Reply(Status.MOVED_PERMANENTLY, headers=(("Location", f"{_BASE}/new"),)),
            Reply(Status.OK),
        ]
    )
    defaults = ServiceDefaults(base_url=_BASE, api_version="2026-01-01")
    result = merge_async_config(
        defaults,
        transport=transport,
        redirect=AsyncRedirectPolicy(),
        retry=AsyncRetryPolicy(backoff_factor=0.0, clock=_AsyncFakeClock()),
    )
    async with AsyncServiceCore(**result) as core:
        response = await core.execute_request(_get())
        await response.close()
    assert len(transport.requests) == 3
    assert all(req.headers.get("Api-Version") == "2026-01-01" for req in transport.requests)


# --------------------------------------------------------------------------- #
# BYO-nesting close matrix                                                      #
# --------------------------------------------------------------------------- #


def test_byo_transport_survives_service_executor_close() -> None:
    # merge_config always folds a fresh transport into a customized pipeline
    # handed to ServiceCore as `pipeline=` — the ownership-aware close means
    # the transport backing that customization is never auto-closed.
    transport = ScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="svc/1.0", api_version="v1")
    result = merge_config(defaults, transport=transport)
    with ServiceCore(**result):
        pass
    assert transport.close_calls == 0


async def test_byo_transport_survives_async_service_executor_close() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK)])
    defaults = ServiceDefaults(base_url=_BASE, user_agent_token="svc/1.0", api_version="v1")
    result = merge_async_config(defaults, transport=transport)
    async with AsyncServiceCore(**result):
        pass
    assert transport.aclose_calls == 0


def test_byo_already_built_pipeline_passes_through_unmodified() -> None:
    transport = ScriptTransport([Reply(Status.OK)])
    byo_pipeline = Pipeline(transport)
    defaults = ServiceDefaults(base_url=_BASE, api_version="v1")
    result = merge_config(defaults, pipeline=byo_pipeline)
    assert result["pipeline"] is byo_pipeline
    with ServiceCore(**result):
        pass
    assert transport.close_calls == 0


async def test_byo_already_built_async_pipeline_passes_through_unmodified() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK)])
    byo_pipeline = AsyncPipeline(transport)
    defaults = ServiceDefaults(base_url=_BASE, api_version="v1")
    result = merge_async_config(defaults, pipeline=byo_pipeline)
    assert result["pipeline"] is byo_pipeline
    async with AsyncServiceCore(**result):
        pass
    assert transport.aclose_calls == 0
