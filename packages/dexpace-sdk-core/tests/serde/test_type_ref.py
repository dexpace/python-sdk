# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``TypeRef`` — validated runtime type witnesses.

Imports come straight from ``dexpace.sdk.core.serde.type_ref`` and
``...serde.codec`` rather than the package ``__init__`` so the suite does not
depend on the re-export landing.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import ParamSpec, TypeVar, TypeVarTuple

import pytest

from dexpace.sdk.core.serde.codec import Codec
from dexpace.sdk.core.serde.type_ref import TypeRef


@dataclass(frozen=True, slots=True)
class _Pet:
    name: str


# PEP 695 alias statements — their ``__value__`` is lazy and must be forced by
# ``TypeRef`` at construction time.
type _Pets = list[_Pet]
type _PetsAlias = _Pets


@pytest.fixture
def codec() -> Codec:
    return Codec()


# --------------------------------------------------------------------------- #
# Element-type information is preserved and usable                            #
# --------------------------------------------------------------------------- #


def test_subscripted_generic_witness_exposes_origin_and_args() -> None:
    ref = TypeRef(list[_Pet])
    assert ref.origin is list
    assert ref.args == (_Pet,)
    assert ref.witness == list[_Pet]


def test_mapping_witness_exposes_key_and_value_args() -> None:
    ref = TypeRef(dict[str, _Pet])
    assert ref.origin is dict
    assert ref.args == (str, _Pet)


def test_decode_through_type_ref_recovers_element_models(codec: Codec) -> None:
    # The whole point: the element type survives through to decoding, so each
    # wire object is reconstructed as a ``_Pet`` rather than left as a dict.
    ref = TypeRef(list[_Pet])
    decoded = codec.decode([{"name": "rex"}, {"name": "mia"}], ref)
    assert decoded == [_Pet("rex"), _Pet("mia")]
    assert all(isinstance(p, _Pet) for p in decoded)


def test_decode_through_dict_witness_recovers_values(codec: Codec) -> None:
    ref = TypeRef(dict[str, _Pet])
    decoded = codec.decode({"a": {"name": "rex"}}, ref)
    assert decoded == {"a": _Pet("rex")}


def test_plain_class_witness_decodes_single_model(codec: Codec) -> None:
    ref = TypeRef(_Pet)
    assert codec.decode({"name": "rex"}, ref) == _Pet("rex")


def test_plain_scalar_witness_is_accepted() -> None:
    # A concrete, non-generic type is a perfectly good witness; guard against
    # the rejection rule being over-broad.
    assert TypeRef(int).witness is int
    assert TypeRef(str).origin is None


# --------------------------------------------------------------------------- #
# Bare type variables are rejected at construction                            #
# --------------------------------------------------------------------------- #


def test_bare_typevar_is_rejected_naming_it() -> None:
    Elem = TypeVar("Elem")
    with pytest.raises(TypeError, match="Elem") as info:
        TypeRef(Elem)  # type: ignore[arg-type]
    assert "type parameter" in str(info.value)


def test_bare_paramspec_is_rejected() -> None:
    PS = ParamSpec("PS")  # noqa: N806
    with pytest.raises(TypeError, match="PS"):
        TypeRef(PS)  # type: ignore[arg-type]


def test_bare_typevartuple_is_rejected() -> None:
    TT = TypeVarTuple("TT")  # noqa: N806
    with pytest.raises(TypeError, match="TT"):
        TypeRef(TT)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Unparameterized generic aliases are rejected at construction                #
# --------------------------------------------------------------------------- #


def test_unparameterized_typing_alias_is_rejected() -> None:
    with pytest.raises(TypeError, match="List") as info:
        TypeRef(typing.List)  # noqa: UP006
    assert "unparameterized" in str(info.value)


def test_unparameterized_typing_dict_is_rejected() -> None:
    with pytest.raises(TypeError):
        TypeRef(typing.Dict)  # noqa: UP006


# --------------------------------------------------------------------------- #
# PEP 695 type-alias statements resolve through TypeRef                        #
# --------------------------------------------------------------------------- #


def test_pep695_alias_resolves_to_underlying_witness() -> None:
    # A PEP 695 ``type`` alias is a live value, but statically it is not a
    # ``type[T]``; the runtime resolution is exactly what is under test here.
    ref: TypeRef[list[_Pet]] = TypeRef(_Pets)  # type: ignore[arg-type]
    assert ref.origin is list
    assert ref.args == (_Pet,)


def test_pep695_alias_decodes_like_the_underlying_generic(codec: Codec) -> None:
    ref: TypeRef[list[_Pet]] = TypeRef(_Pets)  # type: ignore[arg-type]
    decoded = codec.decode([{"name": "rex"}], ref)
    assert decoded == [_Pet("rex")]


def test_pep695_alias_to_alias_resolves_transitively() -> None:
    # ``_PetsAlias`` -> ``_Pets`` -> ``list[_Pet]``; every hop must be forced.
    ref: TypeRef[list[_Pet]] = TypeRef(_PetsAlias)  # type: ignore[arg-type]
    assert ref.origin is list
    assert ref.args == (_Pet,)


def test_pathologically_deep_alias_chain_is_rejected_not_hung() -> None:
    # A cyclic alias (``type X = X``) or an absurdly deep alias chain must not
    # spin forever; the resolution guard trips with a clear error. Built at
    # runtime so the pathological nesting stays invisible to the type-checker.
    deep: object = int
    for i in range(150):
        deep = typing.TypeAliasType(f"_Deep{i}", deep)  # type: ignore[misc]
    with pytest.raises(TypeError, match="cyclic"):
        TypeRef(deep)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Value semantics                                                             #
# --------------------------------------------------------------------------- #


def test_equal_witnesses_compare_equal() -> None:
    assert TypeRef(list[_Pet]) == TypeRef(list[_Pet])
    # Distinct witnesses are unequal; the static params differ, so silence the
    # (correct) non-overlap note — the runtime inequality is the point.
    assert TypeRef(list[_Pet]) != TypeRef(list[int])  # type: ignore[comparison-overlap]


def test_type_ref_is_hashable() -> None:
    assert TypeRef(list[_Pet]) in {TypeRef(list[_Pet])}


def test_repr_names_the_witness() -> None:
    assert "_Pet" in repr(TypeRef(list[_Pet]))
