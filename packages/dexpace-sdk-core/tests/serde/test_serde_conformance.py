# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance suite for the strict JSON binder and the serde failure model.

Certifies the shipped ``Codec`` + ``JsonSerde`` against the strict coercion
table, the ``SerdeError`` failure taxonomy, codec hygiene, and the streaming /
buffer profile. Imports go through the package ``__init__`` re-exports, which
form the public serde surface.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest

from dexpace.sdk.core.errors import (
    DeserializationError,
    SdkError,
    SerdeError,
    SerializationError,
)
from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.serde import (
    JSON_SERDE,
    Codec,
    CodecError,
    JsonDeserializer,
    JsonSerde,
    JsonSerializer,
)


@dataclass(frozen=True, slots=True)
class _Inner:
    x: int


@dataclass(frozen=True, slots=True)
class _Event:
    name: str
    at: datetime


@pytest.fixture
def codec() -> Codec:
    return Codec()


# --------------------------------------------------------------------------- #
# Strict coercion table                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SERDE-22")
@pytest.mark.parametrize(
    ("wire", "target", "expected", "expected_type"),
    [
        ("5", int, "5", str),  # string is NOT coerced into int
        (1.5, int, 1.5, float),  # float is NOT truncated into int
        (1, float, 1.0, float),  # int IS widened into float
        (1.0, float, 1.0, float),  # float stays float
        (5, int, 5, int),  # int stays int
        ("", str, "", str),  # empty string is a permitted str
        ("hello", str, "hello", str),  # non-empty string passes through
    ],
)
def test_coercion_matrix(
    codec: Codec,
    wire: object,
    target: type,
    expected: object,
    expected_type: type,
) -> None:
    result: object = codec.decode(wire, target)
    assert result == expected
    # Type identity matters: no-coercion rules must not silently change the
    # Python type, and int->float widening must actually yield a float.
    assert type(result) is expected_type


@pytest.mark.req("SERDE-22")
def test_int_into_float_field_is_widened(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class HasFloat:
        ratio: float

    decoded = codec.decode({"ratio": 3}, HasFloat)
    assert decoded.ratio == 3.0
    assert type(decoded.ratio) is float


@pytest.mark.req("SERDE-22")
def test_bool_into_float_is_not_widened(codec: Codec) -> None:
    # ``bool`` is only nominally an ``int``; widening it to ``float`` would be a
    # coercion, so it passes through untouched.
    result: object = codec.decode(True, float)
    assert result is True


@pytest.mark.req("SERDE-23")
def test_unknown_wire_fields_are_ignored(codec: Codec) -> None:
    decoded = codec.decode({"x": 1, "future_field": "ignored"}, _Inner)
    assert decoded == _Inner(1)


@pytest.mark.req("SERDE-24")
def test_iso8601_datetime_round_trips(codec: Codec) -> None:
    model = _Event("launch", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    document = codec.encode(model)
    assert isinstance(document, dict)
    assert document["at"] == "2026-01-02T03:04:05+00:00"
    assert codec.decode(document, _Event) == model


# --------------------------------------------------------------------------- #
# null into a non-Optional target names the target type (every overload)       #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SERDE-13")
@pytest.mark.parametrize("target", [str, int, float, bool, _Inner])
def test_null_into_non_optional_type_target_names_target(codec: Codec, target: type) -> None:
    # The direct type-target path: decoding ``null`` against a concrete,
    # non-optional target names the target type in the error.
    with pytest.raises(CodecError) as info:
        codec.decode(None, target)
    assert target.__name__ in str(info.value)


@pytest.mark.req("SERDE-13")
def test_null_into_non_optional_dataclass_field_names_target(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class HasName:
        name: str

    with pytest.raises(CodecError) as info:
        codec.decode({"name": None}, HasName)
    assert "str" in str(info.value)


@pytest.mark.req("SERDE-13")
def test_null_into_non_optional_list_element_names_target(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode([None], list[str])
    assert "str" in str(info.value)


def test_null_into_non_optional_tuple_element_names_target(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode([None], tuple[str])
    assert "str" in str(info.value)


def test_null_into_non_optional_set_element_names_target(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode([None], set[str])
    assert "str" in str(info.value)


@pytest.mark.req("SERDE-13")
def test_null_into_non_optional_mapping_value_names_target(codec: Codec) -> None:
    with pytest.raises(CodecError) as info:
        codec.decode({"a": None}, dict[str, int])
    assert "int" in str(info.value)


def test_null_into_optional_field_is_accepted(codec: Codec) -> None:
    @dataclass(frozen=True, slots=True)
    class HasOptional:
        name: str | None = None

    assert codec.decode({"name": None}, HasOptional).name is None


# --------------------------------------------------------------------------- #
# SerdeError failure taxonomy                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-23", "SERDE-10")
def test_serde_error_roots_the_taxonomy() -> None:
    assert issubclass(SerializationError, SerdeError)
    assert issubclass(DeserializationError, SerdeError)
    assert issubclass(SerdeError, SdkError)
    assert issubclass(SerdeError, ValueError)
    # CodecError stays a decode-side failure.
    assert issubclass(CodecError, DeserializationError)


@pytest.mark.req("SERDE-10")
def test_serde_error_subtypes_are_directional() -> None:
    assert not issubclass(SerializationError, DeserializationError)
    assert not issubclass(DeserializationError, SerializationError)


@pytest.mark.req("SEAM-23", "SERDE-11")
def test_serde_errors_are_unchecked() -> None:
    # "Unchecked" in the Python sense: an ordinary exception nothing forces a
    # caller to catch.
    assert issubclass(SerdeError, Exception)


@pytest.mark.req("SERDE-10")
def test_catching_serde_error_catches_both_directions() -> None:
    caught: list[str] = []
    try:
        JSON_SERDE.deserializer.deserialize("{not json")
    except SerdeError:
        caught.append("decode")
    try:
        JsonSerializer().serialize({"x": object()})
    except SerdeError:
        caught.append("encode")
    assert caught == ["decode", "encode"]


@pytest.mark.req("SEAM-21", "SERDE-9")
def test_deserialization_error_chains_cause() -> None:
    with pytest.raises(DeserializationError) as info:
        JSON_SERDE.deserializer.deserialize("{not json")
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


@pytest.mark.req("SEAM-20", "SERDE-9")
def test_serialization_error_chains_cause() -> None:
    with pytest.raises(SerializationError) as info:
        JsonSerializer().serialize({"k": b"\xff\xfe"})
    assert info.value.__cause__ is not None


@pytest.mark.req("SEAM-23", "SERDE-9")
def test_unserializable_value_is_wrapped_not_leaked() -> None:
    # A stdlib TypeError from json must not escape the serde boundary unwrapped.
    with pytest.raises(SerializationError):
        JsonSerializer().serialize({"x": object()})


# --------------------------------------------------------------------------- #
# OSError passes through the serde boundary unwrapped                           #
# --------------------------------------------------------------------------- #


class _BoomStream:
    """A stream whose I/O always fails with a genuine ``OSError``."""

    def read(self, size: int = -1, /) -> bytes:
        raise OSError("simulated read failure")

    def write(self, data: Any, /) -> int:
        raise OSError("simulated write failure")


@pytest.mark.req("SEAM-20", "SERDE-12")
def test_serialize_to_stream_propagates_oserror_unwrapped() -> None:
    with pytest.raises(OSError) as info:
        JsonSerializer().serialize_to_stream({"a": 1}, _BoomStream())  # type: ignore[arg-type]
    assert not isinstance(info.value, SerdeError)


@pytest.mark.req("SEAM-21", "SERDE-12")
def test_deserialize_stream_propagates_oserror_unwrapped() -> None:
    with pytest.raises(OSError) as info:
        JsonDeserializer().deserialize_stream(_BoomStream())  # type: ignore[arg-type]
    assert not isinstance(info.value, SerdeError)


# --------------------------------------------------------------------------- #
# Round-trip through the SDK's own serializer / deserializer pair               #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SERDE-1")
def test_round_trip_through_serde_pair() -> None:
    serde = JsonSerde()
    payload = {"name": "alice", "nums": [1, 2, 3], "nested": {"ok": True}}
    text = serde.serializer.serialize(payload)
    assert serde.deserializer.deserialize(text) == payload


def test_round_trip_model_through_codec_and_serde_pair() -> None:
    serde = JsonSerde()
    codec = Codec()
    model = _Event("launch", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    text = serde.serializer.serialize(codec.encode(model))
    restored = codec.decode(serde.deserializer.deserialize(text), _Event)
    assert restored == model


def test_round_trip_through_bytes_pair() -> None:
    serde = JsonSerde()
    payload = {"hello": "world"}
    raw = serde.serializer.serialize_to_bytes(payload)
    assert serde.deserializer.deserialize_bytes(raw) == payload


# --------------------------------------------------------------------------- #
# Codec hygiene: fresh instances, no mutation, thread-safety                    #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SERDE-26")
def test_serializing_does_not_mutate_caller_supplied_codec() -> None:
    codec = Codec(tolerate_unknown=False)
    before = codec._tolerate_unknown
    for _ in range(4):
        codec.encode(_Inner(1))
        assert codec.decode({"x": 2}, _Inner) == _Inner(2)
    # The only instance state is unchanged, and no stray attribute was added
    # (slots make an accidental write raise, but assert intent explicitly).
    assert codec._tolerate_unknown == before
    assert Codec.__slots__ == ("_tolerate_unknown",)


@pytest.mark.req("SERDE-25")
def test_distinct_codec_instances_do_not_share_state() -> None:
    tolerant = Codec(tolerate_unknown=True)
    strict = Codec(tolerate_unknown=False)
    assert tolerant is not strict
    assert tolerant.decode({"x": 1, "extra": 2}, _Inner) == _Inner(1)
    with pytest.raises(CodecError):
        strict.decode({"x": 1, "extra": 2}, _Inner)


@pytest.mark.req("SERDE-25")
def test_json_serde_factory_returns_independent_bundles() -> None:
    one = JsonSerde()
    two = JsonSerde()
    assert one is not two
    assert one.serializer is not two.serializer
    assert one.deserializer is not two.deserializer


@pytest.mark.req("SERDE-29")
def test_configured_codec_is_thread_safe() -> None:
    codec = Codec()
    doc = {"x": 7}

    def decode_many() -> list[_Inner]:
        return [codec.decode(doc, _Inner) for _ in range(200)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(decode_many) for _ in range(8)]]
    assert all(item == _Inner(7) for batch in results for item in batch)


# --------------------------------------------------------------------------- #
# Media type is declared by the bundle, not defaulted at the SPI boundary       #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-19", "SERDE-2")
def test_json_serde_declares_application_json_media_type() -> None:
    assert JsonSerde().media_type is common_media_types.APPLICATION_JSON


@pytest.mark.req("SERDE-2")
def test_singleton_serde_declares_media_type() -> None:
    assert JSON_SERDE.media_type is common_media_types.APPLICATION_JSON


# --------------------------------------------------------------------------- #
# Streaming / buffer profile                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-20", "SERDE-3")
def test_serialize_to_stream_does_not_close_stream() -> None:
    stream = BytesIO()
    JsonSerializer().serialize_to_stream({"a": 1}, stream)
    assert not stream.closed
    stream.write(b"!")
    assert stream.getvalue() == b'{"a":1}!'


@pytest.mark.req("SERDE-4")
def test_serialize_into_writes_and_returns_length() -> None:
    buffer = bytearray(32)
    written = JsonSerializer().serialize_into({"a": 1}, buffer)
    assert written == len(b'{"a":1}')
    assert bytes(buffer[:written]) == b'{"a":1}'


@pytest.mark.req("SERDE-3", "SERDE-4")
def test_serialize_into_honors_offset() -> None:
    buffer = bytearray(b"HEAD" + b"\x00" * 16)
    written = JsonSerializer().serialize_into({"a": 1}, buffer, offset=4)
    assert bytes(buffer[4 : 4 + written]) == b'{"a":1}'
    assert bytes(buffer[:4]) == b"HEAD"  # region before the offset is untouched


def test_serialize_into_accepts_writable_memoryview() -> None:
    backing = bytearray(16)
    written = JsonSerializer().serialize_into({"a": 1}, memoryview(backing))
    assert bytes(backing[:written]) == b'{"a":1}'


@pytest.mark.req("SEAM-20", "SERDE-4")
def test_serialize_into_overflow_raises_range_error_not_serde_error() -> None:
    buffer = bytearray(3)
    with pytest.raises(ValueError) as info:
        JsonSerializer().serialize_into({"a": 1}, buffer)
    # An overflow is a caller/range error, not a serde-shape failure.
    assert not isinstance(info.value, SerdeError)


@pytest.mark.req("SERDE-4")
def test_serialize_into_negative_offset_raises_value_error() -> None:
    with pytest.raises(ValueError):
        JsonSerializer().serialize_into({"a": 1}, bytearray(16), offset=-1)


def test_serialize_into_unserializable_value_raises_before_writing() -> None:
    buffer = bytearray(64)
    with pytest.raises(SerializationError):
        JsonSerializer().serialize_into({"x": object()}, buffer)
    assert bytes(buffer) == bytes(64)  # nothing was written
