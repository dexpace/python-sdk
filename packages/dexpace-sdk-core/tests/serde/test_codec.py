# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the typed-model codec.

Imports come straight from ``dexpace.sdk.core.serde.codec`` rather than the
package ``__init__`` so the suite does not depend on the re-export landing.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

import pytest

from dexpace.sdk.core.errors import DeserializationError, SerializationError
from dexpace.sdk.core.serde.codec import (
    ALIAS_KEY,
    Codec,
    CodecError,
    discriminated,
    field_alias,
    variant,
)
from dexpace.sdk.core.serde.tristate import ABSENT, NULL, Present, Tristate


class _Color(enum.Enum):
    RED = "red"
    GREEN = "green"


@dataclass(frozen=True, slots=True)
class _Inner:
    x: int


@dataclass(frozen=True, slots=True)
class _Model:
    name: str
    created: datetime
    color: _Color
    inner: _Inner | None = None
    tags: list[str] = field(default_factory=list)
    nick: str = field(default="", metadata={ALIAS_KEY: "nick_name"})
    note: Tristate[str] = ABSENT


_BASE_DOC: dict[str, object] = {
    "name": "alice",
    "created": "2026-01-02T03:04:05Z",
    "color": "red",
    "inner": {"x": 7},
    "tags": ["p", "q"],
    "nick_name": "al",
}


@pytest.fixture
def codec() -> Codec:
    return Codec()


# --------------------------------------------------------------------------- #
# Plain dataclass decode                                                       #
# --------------------------------------------------------------------------- #


def test_decode_populates_all_declared_fields(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    assert model.name == "alice"
    assert model.color is _Color.RED
    assert model.inner == _Inner(7)
    assert model.tags == ["p", "q"]


def test_decode_maps_aliased_field_from_wire_name(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    assert model.nick == "al"


def test_decode_parses_datetime_with_trailing_z(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    assert model.created == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_decode_applies_field_default_when_key_missing(codec: Codec) -> None:
    doc = {k: v for k, v in _BASE_DOC.items() if k != "tags"}
    model = codec.decode(doc, _Model)
    assert model.tags == []


def test_decode_raises_when_required_field_missing(codec: Codec) -> None:
    doc = {"created": "2026-01-01T00:00:00", "color": "red"}
    with pytest.raises(CodecError) as info:
        codec.decode(doc, _Model)
    assert "name" in str(info.value)


def test_decode_raises_when_dataclass_target_is_not_a_mapping(codec: Codec) -> None:
    with pytest.raises(CodecError):
        codec.decode(["not", "an", "object"], _Model)


# --------------------------------------------------------------------------- #
# Unknown-key tolerance                                                        #
# --------------------------------------------------------------------------- #


def test_decode_tolerates_unknown_keys_by_default(codec: Codec) -> None:
    model = codec.decode({**_BASE_DOC, "future_field": 1}, _Model)
    assert model.name == "alice"


def test_decode_rejects_unknown_keys_when_configured() -> None:
    strict = Codec(tolerate_unknown=False)
    with pytest.raises(CodecError) as info:
        strict.decode({**_BASE_DOC, "future_field": 1}, _Model)
    assert "future_field" in str(info.value)


# --------------------------------------------------------------------------- #
# Tristate fields                                                              #
# --------------------------------------------------------------------------- #


def test_decode_missing_tristate_key_uses_default_absent(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    assert model.note is ABSENT


def test_decode_present_tristate_value_wraps_in_present(codec: Codec) -> None:
    model = codec.decode({**_BASE_DOC, "note": "hi"}, _Model)
    assert model.note == Present("hi")


def test_decode_null_tristate_value_becomes_null(codec: Codec) -> None:
    model = codec.decode({**_BASE_DOC, "note": None}, _Model)
    assert model.note is NULL


def test_encode_omits_absent_tristate_field(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    assert "note" not in codec.encode(model)  # type: ignore[operator]


def test_encode_writes_null_for_null_tristate_field(codec: Codec) -> None:
    model = codec.decode({**_BASE_DOC, "note": None}, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["note"] is None


def test_encode_writes_value_for_present_tristate_field(codec: Codec) -> None:
    model = codec.decode({**_BASE_DOC, "note": "hi"}, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["note"] == "hi"


def test_decode_tristate_recurses_into_inner_type() -> None:
    @dataclass(frozen=True, slots=True)
    class Wrapped:
        when: Tristate[datetime] = ABSENT

    model = Codec().decode({"when": "2026-01-02T03:04:05Z"}, Wrapped)
    assert model.when == Present(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# Encode round-trip                                                            #
# --------------------------------------------------------------------------- #


def test_encode_uses_wire_name_for_aliased_field(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["nick_name"] == "al"
    assert "nick" not in encoded


def test_encode_emits_iso_string_for_datetime(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["created"] == "2026-01-02T03:04:05+00:00"


def test_encode_emits_enum_value(codec: Codec) -> None:
    model = codec.decode(_BASE_DOC, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["color"] == "red"


def test_encode_optional_none_is_written_as_null(codec: Codec) -> None:
    doc = {k: v for k, v in _BASE_DOC.items() if k != "inner"}
    model = codec.decode(doc, _Model)
    encoded = codec.encode(model)
    assert isinstance(encoded, dict)
    assert encoded["inner"] is None


def test_encode_rejects_non_documentable_value(codec: Codec) -> None:
    with pytest.raises(SerializationError):
        codec.encode(object())


# --------------------------------------------------------------------------- #
# Containers and scalars                                                       #
# --------------------------------------------------------------------------- #


def test_decode_list_of_models(codec: Codec) -> None:
    decoded = codec.decode([{"x": 1}, {"x": 2}], list[_Inner])
    assert decoded == [_Inner(1), _Inner(2)]


def test_decode_dict_of_models(codec: Codec) -> None:
    decoded = codec.decode({"a": {"x": 1}, "b": {"x": 2}}, dict[str, _Inner])
    assert decoded == {"a": _Inner(1), "b": _Inner(2)}


def test_decode_homogeneous_tuple(codec: Codec) -> None:
    assert codec.decode([1, 2, 3], tuple[int, ...]) == (1, 2, 3)


def test_decode_set(codec: Codec) -> None:
    assert codec.decode([1, 2, 2, 3], set[int]) == {1, 2, 3}


def test_decode_frozenset(codec: Codec) -> None:
    assert codec.decode([1, 2], frozenset[int]) == frozenset({1, 2})


def test_decode_optional_none_passthrough(codec: Codec) -> None:
    # Unions are decoded at runtime; the static type[T] signature can't model them.
    assert codec.decode(None, str | None) is None  # type: ignore[arg-type]


def test_decode_optional_decodes_inner(codec: Codec) -> None:
    assert codec.decode({"x": 4}, _Inner | None) == _Inner(4)  # type: ignore[arg-type]


def test_decode_scalars_pass_through_without_coercion(codec: Codec) -> None:
    # Scalars pass through uncoerced: decoding "5" as int yields the original str.
    assert codec.decode("5", int) == "5"  # type: ignore[comparison-overlap]
    assert codec.decode(5, int) == 5
    assert codec.decode(True, bool) is True


def test_decode_object_target_is_passthrough(codec: Codec) -> None:
    payload = {"arbitrary": [1, 2]}
    assert codec.decode(payload, object) is payload


def test_decode_sequence_rejects_non_array(codec: Codec) -> None:
    with pytest.raises(CodecError):
        codec.decode("string-not-array", list[str])


# --------------------------------------------------------------------------- #
# Date / time                                                                  #
# --------------------------------------------------------------------------- #


def test_decode_date(codec: Codec) -> None:
    assert codec.decode("2026-06-09", date) == date(2026, 6, 9)


def test_decode_time(codec: Codec) -> None:
    assert codec.decode("12:30:00", time) == time(12, 30)


def test_decode_invalid_datetime_raises_with_path(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode({**_BASE_DOC, "created": "not-a-date"}, _Model)
    assert "created" in str(info.value)


def test_decode_invalid_enum_raises(codec: Codec) -> None:
    with pytest.raises(CodecError):
        codec.decode({**_BASE_DOC, "color": "purple"}, _Model)


# --------------------------------------------------------------------------- #
# Discriminated unions                                                         #
# --------------------------------------------------------------------------- #


@discriminated("type")
class _Pay:
    pass


@variant("card")
@dataclass(frozen=True, slots=True)
class _Card(_Pay):
    last4: str
    type: str = "card"


@variant("bank")
@dataclass(frozen=True, slots=True)
class _Bank(_Pay):
    iban: str
    type: str = "bank"


def test_decode_dispatches_to_variant_by_tag(codec: Codec) -> None:
    decoded = codec.decode({"type": "card", "last4": "1234"}, _Pay)
    assert isinstance(decoded, _Card)
    assert decoded.last4 == "1234"


def test_decode_list_of_union_variants(codec: Codec) -> None:
    decoded = codec.decode(
        [{"type": "bank", "iban": "X"}, {"type": "card", "last4": "9"}],
        list[_Pay],
    )
    assert isinstance(decoded[0], _Bank)
    assert isinstance(decoded[1], _Card)


def test_decode_unknown_tag_raises_listing_known(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode({"type": "crypto"}, _Pay)
    message = str(info.value)
    assert "crypto" in message
    assert "card" in message


def test_decode_missing_discriminator_raises(codec: Codec) -> None:
    with pytest.raises(CodecError):
        codec.decode({"last4": "1"}, _Pay)


def test_encode_variant_emits_discriminator_field(codec: Codec) -> None:
    encoded = codec.encode(_Card(last4="1234"))
    assert encoded == {"last4": "1234", "type": "card"}


def test_variant_duplicate_tag_raises() -> None:
    with pytest.raises(ValueError, match="already registered"):
        variant("card")(_Card)


def test_variant_without_discriminated_base_raises() -> None:
    @dataclass(frozen=True, slots=True)
    class Orphan:
        v: int

    with pytest.raises(TypeError):
        variant("z")(Orphan)


# --------------------------------------------------------------------------- #
# Error model                                                                  #
# --------------------------------------------------------------------------- #


def test_codec_error_is_a_deserialization_error() -> None:
    assert issubclass(CodecError, DeserializationError)
    assert issubclass(CodecError, ValueError)


def test_codec_error_renders_nested_path(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class Outer:
        items: list[_Inner]

    with pytest.raises(CodecError) as info:
        codec.decode({"items": [{"x": 1}, "oops"]}, Outer)
    assert "items[1]" in str(info.value)


def test_codec_error_carries_path_tuple(codec: Codec) -> None:
    err = CodecError("boom", path=("a", "[0]", "b"), target_name="X")
    assert err.path == ("a", "[0]", "b")
    assert "a[0].b" in str(err)


# --------------------------------------------------------------------------- #
# field_alias helper                                                           #
# --------------------------------------------------------------------------- #


def test_field_alias_sets_metadata() -> None:
    @dataclass(frozen=True, slots=True)
    class Aliased:
        value: int = field_alias("v", default=3)  # type: ignore[assignment]

    fields = {f.name: f for f in dataclasses.fields(Aliased)}
    assert fields["value"].metadata[ALIAS_KEY] == "v"


def test_field_alias_default_is_used_when_key_absent(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class Aliased:
        value: int = field_alias("v", default=3)  # type: ignore[assignment]

    assert codec.decode({}, Aliased).value == 3
    assert codec.decode({"v": 9}, Aliased).value == 9


def test_field_alias_default_factory() -> None:
    @dataclass(frozen=True, slots=True)
    class Aliased:
        # field_alias returns a dataclasses.Field, same as dataclasses.field().
        items: list[int] = field_alias("xs", default_factory=list)  # type: ignore[assignment] # noqa: RUF009

    decoded = Codec().decode({}, Aliased)
    assert decoded.items == []


# --------------------------------------------------------------------------- #
# Enum encoding (StrEnum / IntEnum collapse to scalar value)                   #
# --------------------------------------------------------------------------- #


class _StrFlavour(enum.StrEnum):
    A = "aValue"
    B = "bValue"


class _IntLevel(enum.IntEnum):
    LOW = 1
    HIGH = 9


def test_encode_str_enum_member_collapses_to_value(codec: Codec) -> None:
    encoded = codec.encode(_StrFlavour.A)
    assert encoded == "aValue"
    assert type(encoded) is str


def test_encode_int_enum_member_collapses_to_value(codec: Codec) -> None:
    encoded = codec.encode(_IntLevel.HIGH)
    assert encoded == 9
    assert type(encoded) is int


def test_encode_str_enum_field_inside_dataclass(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class Holder:
        flavour: _StrFlavour

    encoded = codec.encode(Holder(_StrFlavour.B))
    assert isinstance(encoded, dict)
    assert encoded["flavour"] == "bValue"
    assert type(encoded["flavour"]) is str


def test_str_enum_round_trips(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class Holder:
        flavour: _StrFlavour

    model = Holder(_StrFlavour.A)
    decoded = codec.decode(codec.encode(model), Holder)
    assert decoded == model


# --------------------------------------------------------------------------- #
# Discriminated tag is exempt from strict unknown-key rejection               #
# --------------------------------------------------------------------------- #


@discriminated("kind")
class _Shape:
    pass


@variant("circle")
@dataclass(frozen=True, slots=True)
class _Circle(_Shape):
    radius: int


def test_strict_codec_accepts_discriminator_without_matching_field() -> None:
    strict = Codec(tolerate_unknown=False)
    decoded = strict.decode({"kind": "circle", "radius": 3}, _Shape)
    assert isinstance(decoded, _Circle)
    assert decoded.radius == 3


def test_strict_codec_still_rejects_genuine_unknown_in_variant() -> None:
    strict = Codec(tolerate_unknown=False)
    with pytest.raises(CodecError) as info:
        strict.decode({"kind": "circle", "radius": 3, "stray": 1}, _Shape)
    assert "stray" in str(info.value)


def test_tolerant_codec_dispatches_discriminator_normally(codec: Codec) -> None:
    decoded = codec.decode({"kind": "circle", "radius": 5}, _Shape)
    assert isinstance(decoded, _Circle)


# --------------------------------------------------------------------------- #
# Fixed-arity tuple length validation                                          #
# --------------------------------------------------------------------------- #


def test_decode_fixed_arity_tuple_exact_length(codec: Codec) -> None:
    assert codec.decode([1, "a"], tuple[int, str]) == (1, "a")


def test_decode_fixed_arity_tuple_too_short_raises(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode([1], tuple[int, str])
    assert "2" in str(info.value)


def test_decode_fixed_arity_tuple_too_long_raises(codec: Codec) -> None:
    with pytest.raises(CodecError):
        codec.decode([1, "a", "EXTRA", 9], tuple[int, str])


def test_decode_homogeneous_tuple_still_unbounded(codec: Codec) -> None:
    assert codec.decode([1, 2, 3, 4], tuple[int, ...]) == (1, 2, 3, 4)


# --------------------------------------------------------------------------- #
# dict key-type recovery                                                       #
# --------------------------------------------------------------------------- #


class _KeyKind(enum.Enum):
    FIRST = "first"
    SECOND = "second"


def test_decode_dict_recovers_enum_keys(codec: Codec) -> None:
    # Enum key types are recovered exactly as enum value types are: the wire
    # key string is mapped to its enum member.
    decoded = codec.decode({"first": 10}, dict[_KeyKind, int])
    assert decoded == {_KeyKind.FIRST: 10}
    assert all(isinstance(k, _KeyKind) for k in decoded)


def test_decode_dict_str_keys_pass_through(codec: Codec) -> None:
    decoded = codec.decode({"a": 1}, dict[str, int])
    assert decoded == {"a": 1}


def test_decode_dict_scalar_keys_follow_value_no_coercion_rule(codec: Codec) -> None:
    # The codec performs no scalar coercion (decode("5", int) == "5"); keys
    # follow the same rule as values, so a wire ``"1"`` key stays a string.
    decoded = codec.decode({"1": "a"}, dict[int, str])
    assert decoded == {"1": "a"}  # type: ignore[comparison-overlap]


def test_decode_dict_recovers_enum_keys_and_model_values(codec: Codec) -> None:
    decoded = codec.decode({"second": {"x": 5}}, dict[_KeyKind, _Inner])
    assert decoded == {_KeyKind.SECOND: _Inner(5)}


# --------------------------------------------------------------------------- #
# Union None-passthrough gating                                                #
# --------------------------------------------------------------------------- #


def test_decode_non_optional_union_does_not_accept_none(codec: Codec) -> None:
    # ``int | str`` has no ``NoneType`` arm; ``None`` must not be injected.
    assert codec.decode(None, int | str) is None  # type: ignore[arg-type]


def test_decode_optional_union_accepts_none_when_arm_present(codec: Codec) -> None:
    assert codec.decode(None, int | None) is None  # type: ignore[arg-type]


def test_decode_optional_union_recovers_inner_dataclass(codec: Codec) -> None:
    assert codec.decode({"x": 4}, _Inner | None) == _Inner(4)  # type: ignore[arg-type]


def test_decode_multi_arm_union_passes_scalar_through(codec: Codec) -> None:
    # A multi-arm union with no matching coercion passes the scalar through.
    assert codec.decode("hello", int | str) == "hello"  # type: ignore[arg-type, comparison-overlap]
