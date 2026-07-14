# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""End-to-end canary for the generated petstore SDK against the head core.

Drives the generated facades — sync and async — over an in-memory transport, so
the whole Tier-1 stack is exercised hermetically (no network). Each scenario
proves one certified capability reaches the caller through the generated facade:

- paginate: ``list_pets`` walks two cursor pages and yields typed `Pet` items;
- events: ``watch_pets`` streams mapped `PetEvent`s and stops on ``[DONE]``;
- typed errors: a 404 raises the `StatusErrorMap`-mapped `PetNotFoundError`, and
  another error status raises the map's default `PetStoreError`;
- auth tiers: the tiered resolver selects the operation-level requirement over
  the client-level default, falls back to the client default when an operation
  declares none, and fails loudly rather than falling through when a present
  tier is unsatisfiable;
- Tristate PATCH: ``update_pet`` round-trips an absent / null / present body,
  the three merge-patch states surviving the wire encode intact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from dexpace.sdk.core.codegen import (
    AsyncServiceCore,
    AuthDescriptor,
    AuthRegistry,
    AuthRequirement,
    AuthResolutionError,
    AuthScheme,
    ServiceCore,
)
from dexpace.sdk.core.codegen._pure.options import AUTH_OPTION_KEY
from dexpace.sdk.core.http.common import Protocol
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage
from dexpace.sdk.core.serde import NULL, present
from dexpace_petstore import (
    AsyncPetStoreClient,
    PetNotFoundError,
    PetStoreClient,
    PetStoreError,
)
from dexpace_petstore.errors import PETSTORE_ERRORS
from dexpace_petstore.models import Pet, PetEvent, PetPatch

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.pipeline.context import PipelineContext

_BASE = "https://api.example.com"


# --------------------------------------------------------------------------- #
# In-memory transports (the simplest fitting scripted-fake pattern)            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Reply:
    """A scripted reply: a status plus an optional body and headers."""

    status: Status
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()


def _sync_response(reply: _Reply, request: Request) -> Response:
    body = ResponseBody.from_bytes(reply.body) if reply.body is not None else None
    response = Response(request=request, protocol=Protocol.HTTP_1_1, status=reply.status, body=body)
    for name, value in reply.headers:
        response = response.with_header(name, value)
    return response


def _async_response(reply: _Reply, request: Request) -> AsyncResponse:
    body = AsyncResponseBody.from_bytes(reply.body) if reply.body is not None else None
    response = AsyncResponse(
        request=request, protocol=Protocol.HTTP_1_1, status=reply.status, body=body
    )
    for name, value in reply.headers:
        response = response.with_header(name, value)
    return response


class _FakeTransport:
    """Sync transport replaying a script; a fresh response is built per call."""

    def __init__(self, script: Sequence[_Reply]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.close_calls = 0

    def execute(self, request: Request) -> Response:
        reply = self._script[min(len(self.requests), len(self._script) - 1)]
        self.requests.append(request)
        return _sync_response(reply, request)

    def close(self) -> None:
        self.close_calls += 1


class _AsyncFakeTransport:
    """Async twin of `_FakeTransport`."""

    def __init__(self, script: Sequence[_Reply]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.aclose_calls = 0

    async def execute(self, request: Request) -> AsyncResponse:
        reply = self._script[min(len(self.requests), len(self._script) - 1)]
        self.requests.append(request)
        return _async_response(reply, request)

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _OptionsProbe(Policy):
    """Records ``ctx.options`` per exchange so the parked auth policy is visible."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.seen.append(ctx.options)
        return self.next.send(request, ctx)


class _AsyncOptionsProbe(AsyncPolicy):
    """Async twin of `_OptionsProbe`."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        self.seen.append(ctx.options)
        return await self.next.send(request, ctx)


class _MarkerAuth(Policy):
    """A stand-in auth policy: only parked in the options mapping, never run."""

    STAGE = Stage.AUTH
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:  # pragma: no cover
        raise NotImplementedError


class _AsyncMarkerAuth(AsyncPolicy):
    """Async twin of `_MarkerAuth`."""

    STAGE = Stage.AUTH
    __slots__ = ()

    async def send(
        self, request: Request, ctx: PipelineContext
    ) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


def _pet_bytes(pet_id: str, name: str) -> bytes:
    return json.dumps({"id": pet_id, "name": name}).encode()


_PAGE_1 = b'{"data": [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}], "next_cursor": "c2"}'
_PAGE_2 = b'{"data": [{"id": "3", "name": "c"}], "next_cursor": null}'


def _sse_body() -> bytes:
    frames = [
        json.dumps({"kind": "created", "pet_id": "1"}),
        json.dumps({"kind": "updated", "pet_id": "1"}),
        "[DONE]",
    ]
    return "".join(f"data: {frame}\n\n" for frame in frames).encode()


# --------------------------------------------------------------------------- #
# paginate                                                                     #
# --------------------------------------------------------------------------- #


def test_paginate_yields_typed_pets_sync() -> None:
    transport = _FakeTransport([_Reply(Status.OK, _PAGE_1), _Reply(Status.OK, _PAGE_2)])
    client = PetStoreClient(ServiceCore(base_url=_BASE, pipeline=Pipeline(transport)))
    pets = list(client.list_pets())
    assert all(isinstance(pet, Pet) for pet in pets)
    assert [pet.name for pet in pets] == ["a", "b", "c"]
    assert len(transport.requests) == 2


async def test_paginate_yields_typed_pets_async() -> None:
    transport = _AsyncFakeTransport([_Reply(Status.OK, _PAGE_1), _Reply(Status.OK, _PAGE_2)])
    client = AsyncPetStoreClient(
        AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    )
    pets = [pet async for pet in client.list_pets()]
    assert all(isinstance(pet, Pet) for pet in pets)
    assert [pet.name for pet in pets] == ["a", "b", "c"]
    assert len(transport.requests) == 2


# --------------------------------------------------------------------------- #
# events                                                                       #
# --------------------------------------------------------------------------- #


def test_events_streams_typed_pet_events_sync() -> None:
    transport = _FakeTransport([_Reply(Status.OK, _sse_body())])
    client = PetStoreClient(ServiceCore(base_url=_BASE, pipeline=Pipeline(transport)))
    with client.watch_pets() as stream:
        events = list(stream)
    assert events == [PetEvent("created", "1"), PetEvent("updated", "1")]


async def test_events_streams_typed_pet_events_async() -> None:
    transport = _AsyncFakeTransport([_Reply(Status.OK, _sse_body())])
    client = AsyncPetStoreClient(
        AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    )
    async with client.watch_pets() as stream:
        events = [event async for event in stream]
    assert events == [PetEvent("created", "1"), PetEvent("updated", "1")]


# --------------------------------------------------------------------------- #
# typed errors via StatusErrorMap                                              #
# --------------------------------------------------------------------------- #


def test_mapped_404_raises_typed_error_sync() -> None:
    transport = _FakeTransport([_Reply(Status.NOT_FOUND, b'{"message": "nope"}')])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport), errors=PETSTORE_ERRORS)
    client = PetStoreClient(core)
    with pytest.raises(PetNotFoundError):
        client.update_pet("7", PetPatch(name=present("Rex")))


def test_unmapped_error_raises_default_sync() -> None:
    transport = _FakeTransport([_Reply(Status.INTERNAL_SERVER_ERROR, b"boom")])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport), errors=PETSTORE_ERRORS)
    client = PetStoreClient(core)
    with pytest.raises(PetStoreError) as excinfo:
        client.update_pet("7", PetPatch(name=present("Rex")))
    assert not isinstance(excinfo.value, PetNotFoundError)


async def test_mapped_404_raises_typed_error_async() -> None:
    transport = _AsyncFakeTransport([_Reply(Status.NOT_FOUND, b'{"message": "nope"}')])
    core = AsyncServiceCore(
        base_url=_BASE, pipeline=AsyncPipeline(transport), errors=PETSTORE_ERRORS
    )
    client = AsyncPetStoreClient(core)
    with pytest.raises(PetNotFoundError):
        await client.update_pet("7", PetPatch(name=present("Rex")))


# --------------------------------------------------------------------------- #
# tiered auth resolver over operation descriptors                             #
# --------------------------------------------------------------------------- #


def _registry() -> tuple[AuthRegistry[Policy], _MarkerAuth, _MarkerAuth]:
    bearer, apikey = _MarkerAuth(), _MarkerAuth()
    registry: AuthRegistry[Policy] = AuthRegistry(
        schemes=(
            AuthScheme("bearer", bearer, frozenset({"pets:read"})),
            AuthScheme("apikey", apikey),
        )
    )
    return registry, bearer, apikey


def test_auth_operation_tier_wins_over_client_default_sync() -> None:
    registry, bearer, _ = _registry()
    probe = _OptionsProbe()
    transport = _FakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=AuthDescriptor.of(AuthRequirement("apikey")),
        auth_registry=registry,
    )
    PetStoreClient(core).get_pet("7")  # get_pet declares an operation-level bearer requirement
    assert probe.seen[-1][AUTH_OPTION_KEY] is bearer


def test_auth_falls_back_to_client_default_sync() -> None:
    registry, _, apikey = _registry()
    probe = _OptionsProbe()
    transport = _FakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=AuthDescriptor.of(AuthRequirement("apikey")),
        auth_registry=registry,
    )
    PetStoreClient(core).update_pet("7", PetPatch(name=present("Rex")))  # no operation-level auth
    assert probe.seen[-1][AUTH_OPTION_KEY] is apikey


def test_auth_present_but_unsatisfiable_fails_loudly_sync() -> None:
    empty: AuthRegistry[Policy] = AuthRegistry()  # no scheme satisfies the operation's bearer
    transport = _FakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport), auth_registry=empty)
    with pytest.raises(AuthResolutionError):
        PetStoreClient(core).get_pet("7")
    assert transport.requests == []  # resolution fails before any request is sent


async def test_auth_operation_tier_wins_over_client_default_async() -> None:
    bearer, apikey = _AsyncMarkerAuth(), _AsyncMarkerAuth()
    registry: AuthRegistry[AsyncPolicy] = AuthRegistry(
        schemes=(
            AuthScheme("bearer", bearer, frozenset({"pets:read"})),
            AuthScheme("apikey", apikey),
        )
    )
    probe = _AsyncOptionsProbe()
    transport = _AsyncFakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    core = AsyncServiceCore(
        base_url=_BASE,
        pipeline=AsyncPipeline(transport, policies=[probe]),
        auth=AuthDescriptor.of(AuthRequirement("apikey")),
        auth_registry=registry,
    )
    await AsyncPetStoreClient(core).get_pet("7")
    assert probe.seen[-1][AUTH_OPTION_KEY] is bearer


# --------------------------------------------------------------------------- #
# Tristate PATCH round-trip                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Captured:
    """Decoded view of the merge-patch body a facade actually sent."""

    document: dict[str, Any] = field(default_factory=dict)
    patch: PetPatch = field(default_factory=PetPatch)


def _capture_patch(request: Request) -> _Captured:
    from dexpace.sdk.core.serde import Codec

    assert request.body is not None
    raw = b"".join(request.body.iter_bytes())
    document = json.loads(raw)
    return _Captured(document=document, patch=Codec().decode(document, PetPatch))


def test_tristate_patch_round_trips_all_three_states_sync() -> None:
    transport = _FakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    client = PetStoreClient(ServiceCore(base_url=_BASE, pipeline=Pipeline(transport)))
    # name -> present, tag -> explicit null, weight_kg -> absent (left at its default).
    client.update_pet("7", PetPatch(name=present("Rex"), tag=NULL))
    captured = _capture_patch(transport.requests[0])
    assert captured.document == {"name": "Rex", "tag": None}  # absent key omitted, null preserved
    assert captured.patch == PetPatch(name=present("Rex"), tag=NULL)


async def test_tristate_patch_round_trips_all_three_states_async() -> None:
    transport = _AsyncFakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    client = AsyncPetStoreClient(
        AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    )
    await client.update_pet("7", PetPatch(name=present("Rex"), tag=NULL))
    captured = _capture_patch(transport.requests[0])
    assert captured.document == {"name": "Rex", "tag": None}
    assert captured.patch == PetPatch(name=present("Rex"), tag=NULL)


# --------------------------------------------------------------------------- #
# lifecycle: closing a facade closes the owned transport                       #
# --------------------------------------------------------------------------- #


def test_facade_close_cascades_to_owned_transport_sync() -> None:
    transport = _FakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    with PetStoreClient(ServiceCore(base_url=_BASE, transport=transport)) as client:
        assert client is not None
    assert transport.close_calls == 1


async def test_facade_close_cascades_to_owned_transport_async() -> None:
    transport = _AsyncFakeTransport([_Reply(Status.OK, _pet_bytes("7", "Rex"))])
    async with AsyncPetStoreClient(AsyncServiceCore(base_url=_BASE, transport=transport)) as client:
        assert client is not None
    assert transport.aclose_calls == 1
