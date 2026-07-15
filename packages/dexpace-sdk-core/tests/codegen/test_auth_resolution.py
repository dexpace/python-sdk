# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the tiered auth-requirement resolver over the M4 stamping policies.

Two layers are exercised. The pure layer (`select_tier` / `resolve_auth`) is
tested without any pipeline: the three-tier matrix (per-call over operation over
client) and — the load-bearing rule — that a present-but-unsatisfiable tier
fails loudly instead of falling through to a satisfiable broader tier. The
executor layer then proves the same rule end-to-end: a bad per-call override
raises before the request is ever sent, so the transport never sees a fallback
request stamped with the wrong (client-default) credential.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.codegen import (
    AsyncServiceCore,
    AuthDescriptor,
    AuthRegistry,
    AuthRequirement,
    AuthResolutionError,
    AuthScheme,
    Operation,
    OperationInput,
    ServiceCore,
)
from dexpace.sdk.core.codegen._pure.options import AUTH_OPTION_KEY
from dexpace.sdk.core.codegen.auth import AuthTier, resolve_auth, select_tier
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.context import PipelineContext
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage

from .support import (
    AsyncOptionsProbe,
    AsyncScriptTransport,
    OptionsProbe,
    Pet,
    Reply,
    ScriptTransport,
)

_BASE = "https://api.example.com"


class _StampPolicy(Policy):
    """A labelled stand-in auth policy — only ever parked, never run."""

    STAGE = Stage.AUTH
    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def send(self, request: Request, ctx: PipelineContext) -> Response:  # pragma: no cover
        raise NotImplementedError


class _AsyncStampPolicy(AsyncPolicy):
    """Async twin of `_StampPolicy`."""

    STAGE = Stage.AUTH
    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    async def send(
        self, request: Request, ctx: PipelineContext
    ) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


def _scheme(name: str, *, scopes: frozenset[str] = frozenset()) -> AuthScheme[Policy]:
    policy: Policy = _StampPolicy(name)
    return AuthScheme(name=name, policy=policy, scopes=scopes)


def _async_scheme(name: str, *, scopes: frozenset[str] = frozenset()) -> AuthScheme[AsyncPolicy]:
    policy: AsyncPolicy = _AsyncStampPolicy(name)
    return AuthScheme(name=name, policy=policy, scopes=scopes)


def _desc(scheme: str, *scopes: str) -> AuthDescriptor:
    return AuthDescriptor.of(AuthRequirement(scheme=scheme, scopes=scopes))


def _get_pet(*, auth: AuthDescriptor | None = None) -> Operation:
    return Operation(name="getPet", method=Method.GET, path="/pets/{id}", auth=auth)


# --------------------------------------------------------------------------- #
# descriptor invariants                                                        #
# --------------------------------------------------------------------------- #


def test_auth_descriptor_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        AuthDescriptor(requirements=())
    with pytest.raises(ValueError, match="at least one"):
        AuthDescriptor.of()


def test_auth_requirement_rejects_blank_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        AuthRequirement(scheme="")


@pytest.mark.req("AUTH-5")
def test_auth_scheme_satisfies_by_name_and_scope_subset() -> None:
    scheme = _scheme("oauth2", scopes=frozenset({"read", "write"}))
    assert scheme.satisfies(AuthRequirement("oauth2", ("read",)))
    assert scheme.satisfies(AuthRequirement("oauth2"))
    assert not scheme.satisfies(AuthRequirement("oauth2", ("admin",)))  # scope not granted
    assert not scheme.satisfies(AuthRequirement("apikey"))  # name mismatch


# --------------------------------------------------------------------------- #
# pure tier selection — presence, most-specific-first                          #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-4", "AUTH-7")
def test_select_tier_prefers_most_specific_present() -> None:
    per_call, operation, client = _desc("a"), _desc("b"), _desc("c")
    assert select_tier(per_call=per_call, operation=operation, client=client) == (
        AuthTier.PER_CALL,
        per_call,
    )
    assert select_tier(per_call=None, operation=operation, client=client) == (
        AuthTier.OPERATION,
        operation,
    )
    assert select_tier(per_call=None, operation=None, client=client) == (AuthTier.CLIENT, client)
    assert select_tier(per_call=None, operation=None, client=None) is None


# --------------------------------------------------------------------------- #
# pure resolution matrix — the selected tier resolves against the registry     #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-7")
def test_resolve_uses_per_call_tier_when_present() -> None:
    registry = AuthRegistry(schemes=(_scheme("bearer"), _scheme("apikey")))
    resolved = resolve_auth(
        per_call=_desc("bearer"),
        operation=_desc("apikey"),
        client=_desc("apikey"),
        registry=registry,
    )
    assert resolved is not None
    assert resolved.tier is AuthTier.PER_CALL
    assert resolved.requirement.scheme == "bearer"


def test_resolve_uses_operation_tier_when_no_per_call() -> None:
    registry = AuthRegistry(schemes=(_scheme("bearer"), _scheme("apikey")))
    resolved = resolve_auth(
        per_call=None, operation=_desc("bearer"), client=_desc("apikey"), registry=registry
    )
    assert resolved is not None
    assert resolved.tier is AuthTier.OPERATION
    assert resolved.requirement.scheme == "bearer"


def test_resolve_uses_client_tier_when_no_per_call_or_operation() -> None:
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    resolved = resolve_auth(
        per_call=None, operation=None, client=_desc("apikey"), registry=registry
    )
    assert resolved is not None
    assert resolved.tier is AuthTier.CLIENT
    assert resolved.requirement.scheme == "apikey"


def test_resolve_returns_none_when_no_tier_present() -> None:
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    assert resolve_auth(per_call=None, operation=None, client=None, registry=registry) is None


@pytest.mark.req("AUTH-5")
def test_resolve_first_satisfiable_requirement_in_ordered_list() -> None:
    # The descriptor lists two alternatives; only the second is registered, so
    # resolution picks it — the ordered list is an OR of alternatives.
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    descriptor = AuthDescriptor.of(AuthRequirement("bearer"), AuthRequirement("apikey"))
    resolved = resolve_auth(per_call=descriptor, operation=None, client=None, registry=registry)
    assert resolved is not None
    assert resolved.requirement.scheme == "apikey"


# --------------------------------------------------------------------------- #
# THE load-bearing rule — a present-but-unsatisfiable tier never falls through #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-4")
def test_unsatisfiable_per_call_does_not_fall_through_to_client() -> None:
    # per-call names a scheme the registry cannot satisfy; the client tier IS
    # satisfiable. Resolution MUST fail against the selected (per-call) tier and
    # never silently authenticate with the client default.
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    with pytest.raises(AuthResolutionError) as excinfo:
        resolve_auth(
            per_call=_desc("bearer"),
            operation=None,
            client=_desc("apikey"),
            registry=registry,
        )
    assert excinfo.value.tier is AuthTier.PER_CALL


@pytest.mark.req("AUTH-4")
def test_unsatisfiable_operation_does_not_fall_through_to_client() -> None:
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    with pytest.raises(AuthResolutionError) as excinfo:
        resolve_auth(
            per_call=None,
            operation=_desc("bearer"),
            client=_desc("apikey"),
            registry=registry,
        )
    assert excinfo.value.tier is AuthTier.OPERATION


def test_unsatisfiable_scope_does_not_fall_through() -> None:
    # Same scheme name, but the requirement demands a scope the scheme cannot
    # grant — still unsatisfiable, still no fall-through.
    registry = AuthRegistry(
        schemes=(_scheme("oauth2", scopes=frozenset({"read"})), _scheme("apikey"))
    )
    with pytest.raises(AuthResolutionError):
        resolve_auth(
            per_call=_desc("oauth2", "admin"),
            operation=None,
            client=_desc("apikey"),
            registry=registry,
        )


# --------------------------------------------------------------------------- #
# executor integration — sync                                                  #
# --------------------------------------------------------------------------- #


def test_executor_parks_resolved_per_call_policy_sync() -> None:
    probe = OptionsProbe()
    registry = AuthRegistry(schemes=(_scheme("bearer"), _scheme("apikey")))
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    core.execute(
        _get_pet(), OperationInput(path_params={"id": "1"}), response_type=Pet, auth=_desc("bearer")
    )
    parked = probe.seen[0][AUTH_OPTION_KEY]
    assert isinstance(parked, _StampPolicy)
    assert parked.label == "bearer"  # per-call tier resolved, not the client default


def test_executor_parks_resolved_operation_policy_sync() -> None:
    probe = OptionsProbe()
    registry = AuthRegistry(schemes=(_scheme("bearer"), _scheme("apikey")))
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    core.execute(
        _get_pet(auth=_desc("bearer")), OperationInput(path_params={"id": "1"}), response_type=Pet
    )
    parked = probe.seen[0][AUTH_OPTION_KEY]
    assert isinstance(parked, _StampPolicy)
    assert parked.label == "bearer"  # operation tier used (no per-call override)


def test_executor_parks_resolved_client_policy_sync() -> None:
    probe = OptionsProbe()
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    core.execute(_get_pet(), OperationInput(path_params={"id": "1"}), response_type=Pet)
    parked = probe.seen[0][AUTH_OPTION_KEY]
    assert isinstance(parked, _StampPolicy)
    assert parked.label == "apikey"  # client default used (no more-specific tier)


def test_executor_byo_policy_override_parked_verbatim_sync() -> None:
    probe = OptionsProbe()
    override = _StampPolicy("byo")
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport, policies=[probe]),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    # A concrete Policy passed per-call is the most-specific tier and is parked
    # verbatim without going through descriptor resolution.
    core.execute(
        _get_pet(), OperationInput(path_params={"id": "1"}), response_type=Pet, auth=override
    )
    assert probe.seen[0][AUTH_OPTION_KEY] is override


def test_executor_unsatisfiable_per_call_raises_before_send_sync() -> None:
    # The client default IS satisfiable, but the per-call override is not: the
    # executor must raise and never dispatch a request stamped with the default.
    registry = AuthRegistry(schemes=(_scheme("apikey"),))
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(
        base_url=_BASE,
        pipeline=Pipeline(transport),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    with pytest.raises(AuthResolutionError) as excinfo:
        core.execute(
            _get_pet(),
            OperationInput(path_params={"id": "1"}),
            response_type=Pet,
            auth=_desc("bearer"),
        )
    assert excinfo.value.tier is AuthTier.PER_CALL
    assert transport.requests == []  # nothing was ever sent


# --------------------------------------------------------------------------- #
# executor integration — async                                                 #
# --------------------------------------------------------------------------- #


async def test_executor_parks_resolved_per_call_policy_async() -> None:
    probe = AsyncOptionsProbe()
    registry = AuthRegistry(schemes=(_async_scheme("bearer"), _async_scheme("apikey")))
    transport = AsyncScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = AsyncServiceCore(
        base_url=_BASE,
        pipeline=AsyncPipeline(transport, policies=[probe]),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    await core.execute(
        _get_pet(), OperationInput(path_params={"id": "1"}), response_type=Pet, auth=_desc("bearer")
    )
    parked = probe.seen[0][AUTH_OPTION_KEY]
    assert isinstance(parked, _AsyncStampPolicy)
    assert parked.label == "bearer"


async def test_executor_unsatisfiable_per_call_raises_before_send_async() -> None:
    registry = AuthRegistry(schemes=(_async_scheme("apikey"),))
    transport = AsyncScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = AsyncServiceCore(
        base_url=_BASE,
        pipeline=AsyncPipeline(transport),
        auth=_desc("apikey"),
        auth_registry=registry,
    )
    with pytest.raises(AuthResolutionError) as excinfo:
        await core.execute(
            _get_pet(),
            OperationInput(path_params={"id": "1"}),
            response_type=Pet,
            auth=_desc("bearer"),
        )
    assert excinfo.value.tier is AuthTier.PER_CALL
    assert transport.requests == []
