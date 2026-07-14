# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Close-ownership matrix for `ServiceCore` and `AsyncServiceCore`.

The executors are ownership-aware context managers: closing one closes a
transport it constructed a pipeline around, but never a caller-supplied (BYO)
pipeline, and a double close is a no-op. Both rows of the matrix are asserted
for both executors.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.codegen import AsyncServiceCore, ServiceCore
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline

from .support import AsyncScriptTransport, Reply, ScriptTransport

_BASE = "https://api.example.com"


def _reply() -> list[Reply]:
    return [Reply(Status.OK)]


# --------------------------------------------------------------------------- #
# BYO pipeline: a caller-supplied resource survives the executor's close        #
# --------------------------------------------------------------------------- #


@pytest.mark.req("XCUT-22")
def test_byo_pipeline_survives_close_sync() -> None:
    transport = ScriptTransport(_reply())
    with ServiceCore(base_url=_BASE, pipeline=Pipeline(transport)) as core:
        assert core is not None
    assert transport.close_calls == 0  # the BYO pipeline (and its transport) is untouched


@pytest.mark.req("XCUT-22")
async def test_byo_pipeline_survives_close_async() -> None:
    transport = AsyncScriptTransport(_reply())
    async with AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport)) as core:
        assert core is not None
    assert transport.aclose_calls == 0


# --------------------------------------------------------------------------- #
# Constructed transport: an executor-built pipeline's transport is closed       #
# --------------------------------------------------------------------------- #


@pytest.mark.req("XCUT-22")
def test_constructed_transport_closed_on_exit_sync() -> None:
    transport = ScriptTransport(_reply())
    with ServiceCore(base_url=_BASE, transport=transport):
        pass
    assert transport.close_calls == 1


async def test_constructed_transport_closed_on_exit_async() -> None:
    transport = AsyncScriptTransport(_reply())
    async with AsyncServiceCore(base_url=_BASE, transport=transport):
        pass
    assert transport.aclose_calls == 1


# --------------------------------------------------------------------------- #
# Double close is a no-op                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.req("XCUT-13")
def test_double_close_is_a_noop_sync() -> None:
    transport = ScriptTransport(_reply())
    core = ServiceCore(base_url=_BASE, transport=transport)
    core.close()
    core.close()
    assert transport.close_calls == 1


@pytest.mark.req("XCUT-13")
async def test_double_close_is_a_noop_async() -> None:
    transport = AsyncScriptTransport(_reply())
    core = AsyncServiceCore(base_url=_BASE, transport=transport)
    await core.aclose()
    await core.aclose()
    assert transport.aclose_calls == 1


# --------------------------------------------------------------------------- #
# Construction requires exactly one of pipeline / transport                     #
# --------------------------------------------------------------------------- #


def test_requires_a_source_sync() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ServiceCore(base_url=_BASE)


def test_rejects_both_sources_sync() -> None:
    transport = ScriptTransport(_reply())
    with pytest.raises(ValueError, match="exactly one"):
        ServiceCore(
            base_url=_BASE, transport=transport, pipeline=Pipeline(ScriptTransport(_reply()))
        )


def test_requires_a_source_async() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        AsyncServiceCore(base_url=_BASE)


def test_rejects_both_sources_async() -> None:
    transport = AsyncScriptTransport(_reply())
    with pytest.raises(ValueError, match="exactly one"):
        AsyncServiceCore(
            base_url=_BASE,
            transport=transport,
            pipeline=AsyncPipeline(AsyncScriptTransport(_reply())),
        )
