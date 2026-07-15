# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Validated runtime type witnesses for the codec.

A *type witness* is an ordinary Python runtime object that stands in for the
type a caller wants a document decoded into. A subscripted generic such as
``list[Pet]`` is a real object at runtime — introspectable with
``typing.get_origin`` / ``typing.get_args`` — so it can carry its element type
all the way into the decode path instead of relying on fragile string hints.

``TypeRef`` is a thin wrapper around such a witness that validates it *eagerly*,
at construction time, so a broken witness fails at the call site that supplied
it rather than deep inside decoding. It rejects two unusable shapes:

- a bare type parameter (``TypeVar`` / ``ParamSpec`` / ``TypeVarTuple``), which
  names no concrete type; and
- an unparameterized generic alias (``typing.List``, ``typing.Dict``, ...),
  which carries no element type.

PEP 695 ``type X = list[Pet]`` alias statements are supported: their
``__value__`` is *lazy*, so ``TypeRef`` forces it (following alias-to-alias
chains) during construction rather than deferring the resolution.

Witnesses are objects, not annotations
--------------------------------------

Because every module in this codebase uses ``from __future__ import
annotations``, user-written annotations on functions and dataclass fields are
*strings* at runtime, not type objects. A witness must therefore be an actual
object passed explicitly at the call site — for example ``response.parse(
list[Pet])`` or ``TypeRef(list[Pet])`` — and can never be harvested from a
function's or class's string annotations. ``TypeRef`` deals exclusively in the
former: live type objects handed to it directly.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import ParamSpec, TypeVar, TypeVarTuple, final, get_args, get_origin

_MAX_ALIAS_DEPTH: typing.Final = 100
"""Ceiling on PEP 695 alias-chain resolution.

A self-referential alias (``type X = X``) yields a ``__value__`` that is the
alias itself, so naive resolution would spin forever. The guard trips instead
and surfaces a clean ``TypeError``.
"""


@final
@dataclasses.dataclass(frozen=True, slots=True, init=False)
class TypeRef[T]:
    """An eagerly validated runtime witness of a target type ``T``.

    Construct one from any concrete type object — a plain class (``Pet``), a
    subscripted generic (``list[Pet]``, ``dict[str, Pet]``), or a PEP 695
    ``type`` alias whose value is such a type. The witness is validated and,
    for a PEP 695 alias, resolved to its underlying value at construction time;
    an unusable witness raises ``TypeError`` immediately.

    The static type parameter ``T`` is a phantom marker: it lets
    ``Codec.decode`` return the witnessed type without a cast at the call site,
    while the resolved witness object is what the codec actually introspects.

    Attributes:
        witness: The resolved runtime type object (aliases already forced).
    """

    witness: object

    def __init__(self, witness: type[T], /) -> None:
        """Resolve and validate ``witness``, storing the concrete type object.

        Args:
            witness: The type object to wrap — a class, a subscripted generic,
                or a PEP 695 ``type`` alias resolving to one.

        Raises:
            TypeError: If ``witness`` is a bare type parameter, an
                unparameterized generic alias, or a cyclic ``type`` alias. The
                message names the offending object.
        """
        resolved = _resolve_alias(witness)
        _reject_unusable(resolved)
        object.__setattr__(self, "witness", resolved)

    @property
    def origin(self) -> object | None:
        """The witness's generic origin, or ``None`` for a plain type.

        For ``list[Pet]`` this is ``list``; for ``Pet`` it is ``None`` — the
        same value ``typing.get_origin`` returns for the resolved witness.
        """
        return get_origin(self.witness)

    @property
    def args(self) -> tuple[object, ...]:
        """The witness's type arguments, e.g. ``(Pet,)`` for ``list[Pet]``.

        Empty for a non-parameterized witness. Mirrors ``typing.get_args`` over
        the resolved witness.
        """
        return get_args(self.witness)


def _resolve_alias(witness: object) -> object:
    """Force a PEP 695 ``type`` alias to its underlying value.

    Follows alias-to-alias chains, since one alias may be defined in terms of
    another. A non-alias witness is returned unchanged.

    Args:
        witness: The candidate witness, possibly a lazy ``TypeAliasType``.

    Returns:
        The witness with every ``type`` alias hop resolved.

    Raises:
        TypeError: If resolution does not terminate within ``_MAX_ALIAS_DEPTH``
            hops — a cyclic alias (``type X = X``) or one nested absurdly deep.
    """
    resolved = witness
    for _ in range(_MAX_ALIAS_DEPTH):
        if not isinstance(resolved, typing.TypeAliasType):
            return resolved
        resolved = resolved.__value__
    raise TypeError(
        f"TypeRef witness {_display(witness)!r} did not resolve to a concrete type "
        f"within {_MAX_ALIAS_DEPTH} alias hops; it may be a cyclic type alias",
    )


def _reject_unusable(witness: object) -> None:
    """Raise if ``witness`` cannot serve as a concrete type witness.

    Args:
        witness: A resolved witness (aliases already forced).

    Raises:
        TypeError: If ``witness`` is a bare type parameter or an
            unparameterized generic alias, with a message naming it.
    """
    if isinstance(witness, (TypeVar, ParamSpec, TypeVarTuple)):
        raise TypeError(
            f"TypeRef witness {_display(witness)!r} is a bare type parameter; "
            f"pass a concrete type such as list[str], not an unbound type variable",
        )
    if get_origin(witness) is not None and not get_args(witness):
        raise TypeError(
            f"TypeRef witness {_display(witness)!r} is an unparameterized generic; "
            f"subscript it with concrete type arguments, e.g. list[str] rather than list",
        )


def _display(witness: object) -> str:
    """Return a readable name for ``witness`` for use in error messages."""
    name = getattr(witness, "__name__", None)
    return name if isinstance(name, str) else repr(witness)


__all__ = ["TypeRef"]
