# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed models for the petstore canary.

Frozen dataclasses the `Codec` reconstructs from wire documents and encodes back.
`PetPatch` is the merge-patch body: each field is a `Tristate`, so the three wire
states an update must distinguish — omit the key (leave unchanged), send an
explicit ``null`` (clear), or send a value (set) — survive a full round trip
rather than collapsing into a single ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from dexpace.sdk.core.serde import ABSENT, Tristate

__all__ = ["Pet", "PetEvent", "PetPatch"]


@dataclass(frozen=True, slots=True)
class Pet:
    """A pet as returned by the service.

    Attributes:
        id: The stable identifier.
        name: The display name.
        tag: An optional free-form tag; ``None`` when the pet carries none.
    """

    id: str
    name: str
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class PetEvent:
    """A single pet lifecycle event delivered over the SSE stream.

    Attributes:
        kind: The event kind (e.g. ``"created"`` / ``"updated"``).
        pet_id: The id of the pet the event concerns.
    """

    kind: str
    pet_id: str


@dataclass(frozen=True, slots=True)
class PetPatch:
    """A merge-patch update body.

    Every field defaults to `ABSENT`, so ``PetPatch(name=present("Rex"))`` sends
    only ``name`` and leaves ``tag`` / ``weight_kg`` untouched. Passing ``NULL``
    for a field clears it on the wire.

    Attributes:
        name: The new name, or ``NULL`` to clear it, or `ABSENT` to leave it.
        tag: The new tag, or ``NULL`` to clear it, or `ABSENT` to leave it.
        weight_kg: The new weight, or ``NULL`` to clear it, or `ABSENT`.
    """

    name: Tristate[str] = ABSENT
    tag: Tristate[str] = ABSENT
    weight_kg: Tristate[float] = ABSENT
