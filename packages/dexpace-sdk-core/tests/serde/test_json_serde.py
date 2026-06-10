# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the JSON ``Serde``."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from io import BytesIO

import pytest

from dexpace.sdk.core.errors import DeserializationError, SerializationError
from dexpace.sdk.core.serde import JSON_SERDE, JsonDeserializer, JsonSerde, JsonSerializer


def test_round_trip_basic_values() -> None:
    payload = {"key": "value", "n": 5, "items": [1, 2, 3]}
    text = JSON_SERDE.serializer.serialize(payload)
    assert JSON_SERDE.deserializer.deserialize(text) == payload


def test_datetime_encoded_as_iso() -> None:
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    text = JSON_SERDE.serializer.serialize({"at": when})
    assert "2024-01-01T12:00:00+00:00" in text


def test_serialize_to_bytes_round_trip() -> None:
    payload = {"hello": "world"}
    raw = JSON_SERDE.serializer.serialize_to_bytes(payload)
    assert isinstance(raw, bytes)
    assert JSON_SERDE.deserializer.deserialize_bytes(raw) == payload


def test_serialize_to_stream() -> None:
    stream = BytesIO()
    JSON_SERDE.serializer.serialize_to_stream({"a": 1}, stream)
    assert stream.getvalue() == b'{"a":1}'


def test_deserialize_stream() -> None:
    stream = BytesIO(b'{"x": "y"}')
    assert JSON_SERDE.deserializer.deserialize_stream(stream) == {"x": "y"}


def test_unsupported_type_raises_serialization_error() -> None:
    class _Unsupported:
        pass

    with pytest.raises(SerializationError):
        JSON_SERDE.serializer.serialize({"obj": _Unsupported()})


def test_malformed_json_raises_deserialization_error() -> None:
    with pytest.raises(DeserializationError):
        JSON_SERDE.deserializer.deserialize("{not json")


def test_serialization_error_is_value_error() -> None:
    try:
        JSON_SERDE.serializer.serialize({"obj": object()})
    except SerializationError as err:
        assert isinstance(err, ValueError)
    else:
        pytest.fail("expected SerializationError")


def test_custom_object_hook() -> None:
    deser = JsonDeserializer(object_hook=lambda d: {**d, "_handled": True})
    result = deser.deserialize('{"a": 1}')
    assert result == {"a": 1, "_handled": True}


def test_sort_keys_emitted_sorted() -> None:
    ser = JsonSerializer(sort_keys=True)
    text = ser.serialize({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'


def test_serde_uses_provided_components() -> None:
    custom_ser = JsonSerializer(sort_keys=True)
    custom_des = JsonDeserializer()
    serde = JsonSerde(custom_ser, custom_des)
    assert serde.serializer is custom_ser
    assert serde.deserializer is custom_des


def test_bytes_encoded_as_utf8_text() -> None:
    text = JSON_SERDE.serializer.serialize({"raw": b"abc"})
    assert '"raw":"abc"' in text


def test_deserialize_bytes_invalid_utf8_raises_deserialization_error() -> None:
    with pytest.raises(DeserializationError):
        JsonDeserializer().deserialize_bytes(b"\xff\xfe")


def test_serialize_invalid_utf8_bytes_raises_serialization_error() -> None:
    with pytest.raises(SerializationError):
        JsonSerializer().serialize({"k": b"\xff\xfe"})


def test_allow_nan_false_rejects_nan() -> None:
    with pytest.raises(SerializationError):
        JsonSerializer().serialize({"v": math.nan})


def test_allow_nan_false_rejects_inf() -> None:
    with pytest.raises(SerializationError):
        JsonSerializer().serialize({"v": math.inf})


def test_allow_nan_true_emits_nan() -> None:
    ser = JsonSerializer(allow_nan=True)
    text = ser.serialize({"v": math.nan})
    assert "NaN" in text


def test_custom_default_still_encodes_builtin_datetime() -> None:
    class _Custom:
        pass

    def my_default(o: object) -> object:
        if isinstance(o, _Custom):
            return "custom"
        raise TypeError(o)

    ser = JsonSerializer(default=my_default)
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    text = ser.serialize({"at": when})
    assert "2024-01-01T12:00:00+00:00" in text


def test_custom_default_handles_its_own_types() -> None:
    class _Custom:
        pass

    def my_default(o: object) -> object:
        if isinstance(o, _Custom):
            return "custom"
        raise TypeError(o)

    ser = JsonSerializer(default=my_default)
    assert ser.serialize({"x": _Custom()}) == '{"x":"custom"}'


def test_custom_default_encodes_both_datetime_and_custom() -> None:
    class _Custom:
        pass

    def my_default(o: object) -> object:
        if isinstance(o, _Custom):
            return "custom"
        raise TypeError(o)

    ser = JsonSerializer(default=my_default)
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    text = ser.serialize({"at": when, "x": _Custom()})
    assert "2024-01-01T12:00:00+00:00" in text
    assert '"x":"custom"' in text


def test_encoder_built_once_and_reused_across_serialize_calls() -> None:
    # The encoder is derived at construction, not per serialize() call, even
    # when a custom default forces a chained encoder subclass.
    def my_default(o: object) -> object:
        raise TypeError(o)

    ser = JsonSerializer(default=my_default)
    first = ser._encoder
    second = ser._encoder
    assert first is second


def test_custom_default_serializer_repeatable_across_calls() -> None:
    class _Custom:
        pass

    def my_default(o: object) -> object:
        if isinstance(o, _Custom):
            return "custom"
        raise TypeError(o)

    ser = JsonSerializer(default=my_default)
    # A serializer reusing one encoder must stay correct across repeated calls.
    assert ser.serialize({"x": _Custom()}) == '{"x":"custom"}'
    assert ser.serialize({"x": _Custom()}) == '{"x":"custom"}'
