# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tiered auth-requirement descriptors and their resolver.

This is pure resolution logic layered *over* the M4 stamping policies in
`http.auth.policies` — it never stamps a credential itself. An operation
declares what authentication it needs as an `AuthDescriptor` (a non-empty,
ordered list of `AuthRequirement` alternatives); the executor resolves that
declaration against an `AuthRegistry` of the concrete stamping policies it has
available, and the chosen policy does the actual header stamping downstream.

Requirements arrive at three tiers, most specific first: **per-call** (the
``auth=`` argument to ``execute``), then **operation-level** (declared on the
`Operation`), then **client-level** (configured on the executor). Resolution
picks the most specific tier that is *present* and resolves strictly against
that one tier:

- Presence — not satisfiability — selects the tier.
- The selected tier must then be satisfiable. If it is present but no
  registered scheme satisfies any of its requirements, resolution raises
  `AuthResolutionError`. It does **not** fall through to a broader tier.

That no-fall-through rule is the whole point of the tiering: a caller who asks
for a specific per-call credential that cannot be honoured must get a loud
failure, never a silent downgrade to the client-level default — which would
authenticate the request with the wrong credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..errors import SdkError

__all__ = [
    "AuthDescriptor",
    "AuthRegistry",
    "AuthRequirement",
    "AuthResolutionError",
    "AuthScheme",
    "AuthTier",
    "ResolvedAuth",
    "resolve_auth",
    "select_tier",
]

#: The scopes an OAuth-style requirement asks for; empty for scopeless schemes.
type Scopes = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthRequirement:
    """A single authentication alternative — a named scheme and its scopes.

    Attributes:
        scheme: The name of a security scheme the registry may satisfy (e.g.
            ``"bearer"``, ``"apikey"``). Must be non-empty.
        scopes: The scopes the requirement asks for. Empty for schemes that do
            not carry scopes (API key, Basic). A scheme satisfies the
            requirement only when it grants every requested scope.
    """

    scheme: str
    scopes: Scopes = ()

    def __post_init__(self) -> None:
        if not self.scheme:
            raise ValueError("AuthRequirement.scheme must be a non-empty scheme name")


#: A non-empty ordered tuple of requirement alternatives (OR semantics).
type AuthRequirements = tuple[AuthRequirement, ...]


@dataclass(frozen=True, slots=True)
class AuthDescriptor:
    """A non-empty ordered list of authentication alternatives.

    The requirements are an OR: a descriptor is satisfied by the *first* of its
    alternatives the registry can satisfy, tried in order. A descriptor with no
    requirements is meaningless and rejected at construction.

    Attributes:
        requirements: The ordered alternatives; must be non-empty.
    """

    requirements: AuthRequirements

    def __post_init__(self) -> None:
        if not self.requirements:
            raise ValueError("AuthDescriptor requires at least one AuthRequirement")

    @classmethod
    def of(cls, *requirements: AuthRequirement) -> AuthDescriptor:
        """Build a descriptor from one or more requirement alternatives.

        Args:
            *requirements: The ordered alternatives.

        Returns:
            A descriptor over ``requirements``.

        Raises:
            ValueError: If no requirements are given.
        """
        return cls(requirements=requirements)


class AuthTier(Enum):
    """Which tier a resolution was drawn from, most specific first."""

    PER_CALL = "per_call"
    OPERATION = "operation"
    CLIENT = "client"


@dataclass(frozen=True, slots=True)
class AuthScheme[P]:
    """A named scheme paired with the stamping policy that realises it.

    The resolver never stamps anything; it selects an `AuthScheme` whose
    ``policy`` (one of the M4 auth policies) is then applied downstream. ``P``
    is the policy type — `Policy` for the sync executor, `AsyncPolicy` for the
    async one.

    Attributes:
        name: The scheme name this policy answers to (matched against
            `AuthRequirement.scheme`).
        policy: The concrete stamping policy the executor applies when this
            scheme is chosen.
        scopes: The scopes this scheme can grant. A requirement is satisfied
            only when its scopes are a subset of these; the default empty set
            models a scopeless scheme (API key, Basic).
    """

    name: str
    policy: P
    scopes: frozenset[str] = frozenset()

    def satisfies(self, requirement: AuthRequirement) -> bool:
        """Report whether this scheme can satisfy ``requirement``.

        Args:
            requirement: The alternative to test.

        Returns:
            ``True`` when the names match and every requested scope is granted.
        """
        return requirement.scheme == self.name and set(requirement.scopes) <= self.scopes


@dataclass(frozen=True, slots=True)
class AuthRegistry[P]:
    """The pool of schemes an executor has available to satisfy requirements.

    Attributes:
        schemes: The available schemes, scanned in order; the first that
            satisfies a requirement wins.
    """

    schemes: tuple[AuthScheme[P], ...] = ()

    def scheme_for(self, requirement: AuthRequirement) -> AuthScheme[P] | None:
        """Return the first registered scheme that satisfies ``requirement``.

        Args:
            requirement: The alternative to satisfy.

        Returns:
            The satisfying scheme, or ``None`` when none is registered for it.
        """
        for scheme in self.schemes:
            if scheme.satisfies(requirement):
                return scheme
        return None


@dataclass(frozen=True, slots=True)
class ResolvedAuth[P]:
    """The outcome of a successful resolution.

    Attributes:
        tier: The tier the requirement was drawn from.
        requirement: The specific alternative that was satisfied.
        scheme: The registered scheme (carrying the stamping ``policy``) chosen
            to satisfy it.
    """

    tier: AuthTier
    requirement: AuthRequirement
    scheme: AuthScheme[P]


class AuthResolutionError(SdkError):
    """A present auth tier could not be satisfied by any registered scheme.

    Raised instead of falling through to a broader tier, so a caller that asked
    for a specific credential learns it could not be honoured rather than
    silently authenticating with a different one.

    Attributes:
        tier: The (most-specific present) tier that failed to resolve.
        descriptor: The unsatisfiable descriptor from that tier.
    """

    tier: AuthTier
    descriptor: AuthDescriptor

    def __init__(self, tier: AuthTier, descriptor: AuthDescriptor) -> None:
        self.tier = tier
        self.descriptor = descriptor
        wanted = ", ".join(sorted({req.scheme for req in descriptor.requirements}))
        super().__init__(
            f"the {tier.value} auth requirement is present but unsatisfiable: no registered "
            f"auth scheme satisfies any of [{wanted}]. Resolution does not fall through to a "
            f"broader tier."
        )


def select_tier(
    *,
    per_call: AuthDescriptor | None,
    operation: AuthDescriptor | None,
    client: AuthDescriptor | None,
) -> tuple[AuthTier, AuthDescriptor] | None:
    """Pick the most-specific *present* tier — presence alone selects it.

    Satisfiability is deliberately not consulted here: a present tier is
    selected even when it cannot be satisfied, so that `resolve_auth` can then
    fail against it rather than fall through to a broader tier.

    Args:
        per_call: The per-call descriptor, or ``None`` if none was given.
        operation: The operation-level descriptor, or ``None``.
        client: The client-level descriptor, or ``None``.

    Returns:
        The selected tier and its descriptor, or ``None`` when every tier is
        absent.
    """
    if per_call is not None:
        return (AuthTier.PER_CALL, per_call)
    if operation is not None:
        return (AuthTier.OPERATION, operation)
    if client is not None:
        return (AuthTier.CLIENT, client)
    return None


def resolve_auth[P](
    *,
    per_call: AuthDescriptor | None,
    operation: AuthDescriptor | None,
    client: AuthDescriptor | None,
    registry: AuthRegistry[P],
) -> ResolvedAuth[P] | None:
    """Resolve the tiered requirements to a single scheme, or raise.

    Selects the most-specific present tier (`select_tier`) and resolves its
    ordered requirements against ``registry``, returning the first that a
    registered scheme satisfies. When the selected tier is present but nothing
    satisfies it, raises `AuthResolutionError` — it never falls through to a
    broader tier.

    Args:
        per_call: The per-call descriptor, or ``None``.
        operation: The operation-level descriptor, or ``None``.
        client: The client-level descriptor, or ``None``.
        registry: The pool of schemes available to satisfy a requirement.

    Returns:
        The resolution, or ``None`` when no tier is present (unauthenticated).

    Raises:
        AuthResolutionError: When the selected tier is present but unsatisfiable.
    """
    selected = select_tier(per_call=per_call, operation=operation, client=client)
    if selected is None:
        return None
    tier, descriptor = selected
    for requirement in descriptor.requirements:
        scheme = registry.scheme_for(requirement)
        if scheme is not None:
            return ResolvedAuth(tier=tier, requirement=requirement, scheme=scheme)
    raise AuthResolutionError(tier, descriptor)
