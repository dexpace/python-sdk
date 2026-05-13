"""Stdlib-backed ``Serde`` for JSON payloads."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any, BinaryIO, Final

from ..errors import DeserializationError, SerializationError


class _JsonEncoder(json.JSONEncoder):
    """Encodes datetimes as ISO-8601; delegates everything else to ``json``."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, time):
            return o.isoformat()
        if isinstance(o, bytes):
            return o.decode("utf-8")
        return super().default(o)


class JsonSerializer:
    """Serialise Python values into JSON strings / bytes / streams."""

    __slots__ = ("_allow_nan", "_default", "_encoder_cls", "_sort_keys")

    def __init__(
        self,
        *,
        default: Callable[[Any], Any] | None = None,
        sort_keys: bool = False,
        allow_nan: bool = False,
        encoder_cls: type[json.JSONEncoder] = _JsonEncoder,
    ) -> None:
        """Configure the serializer.

        Args:
            default: Optional ``default`` callable forwarded to ``json.dumps``.
            sort_keys: When ``True``, object keys are emitted in sorted order
                (useful for stable hashes).
            allow_nan: When ``False`` (the default), ``NaN`` / ``Infinity`` /
                ``-Infinity`` raise ``SerializationError`` instead of emitting
                non-standard JSON tokens.
            encoder_cls: ``json.JSONEncoder`` subclass to use.
        """
        self._default = default
        self._sort_keys = sort_keys
        self._allow_nan = allow_nan
        self._encoder_cls = encoder_cls

    def serialize(self, value: Any) -> str:
        """Serialise ``value`` to a JSON string.

        Raises:
            SerializationError: If encoding fails.
        """
        try:
            return json.dumps(
                value,
                cls=self._encoder_cls,
                default=self._default,
                sort_keys=self._sort_keys,
                allow_nan=self._allow_nan,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, UnicodeDecodeError) as err:
            raise SerializationError(str(err), error=err) from err

    def serialize_to_bytes(self, value: Any) -> bytes:
        """Serialise ``value`` to UTF-8-encoded JSON bytes."""
        return self.serialize(value).encode("utf-8")

    def serialize_to_stream(self, value: Any, stream: BinaryIO) -> None:
        """Serialise ``value`` and write the resulting bytes to ``stream``.

        Does not close ``stream`` — caller retains ownership.
        """
        stream.write(self.serialize_to_bytes(value))


class JsonDeserializer:
    """Deserialise JSON strings / bytes / streams into Python values."""

    __slots__ = ("_object_hook",)

    def __init__(self, *, object_hook: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Configure the deserializer.

        Args:
            object_hook: Optional callback forwarded to ``json.loads`` to
                rebuild domain types from object literals.
        """
        self._object_hook = object_hook

    def deserialize(self, value: str) -> Any:
        """Deserialise a JSON string.

        Raises:
            DeserializationError: If decoding fails.
        """
        try:
            return json.loads(value, object_hook=self._object_hook)
        except json.JSONDecodeError as err:
            raise DeserializationError(str(err), error=err) from err

    def deserialize_bytes(self, value: bytes) -> Any:
        """Deserialise a JSON-encoded ``bytes`` payload.

        Raises:
            DeserializationError: If ``value`` is not valid UTF-8 or its
                decoded text is not valid JSON.
        """
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as err:
            raise DeserializationError(str(err), error=err) from err
        return self.deserialize(text)

    def deserialize_stream(self, stream: BinaryIO) -> Any:
        """Drain ``stream`` to EOF and deserialise its contents."""
        return self.deserialize_bytes(stream.read())


class JsonSerde:
    """Bundle of ``JsonSerializer`` + ``JsonDeserializer``.

    Acts as a single injection point: components needing JSON round-trips
    take one ``JsonSerde`` rather than separate serializer / deserializer
    references. Most callers can use the module-level ``JSON_SERDE``
    singleton.

    Attributes:
        serializer: The JSON encoder.
        deserializer: The JSON decoder.
    """

    __slots__ = ("_deserializer", "_serializer")

    def __init__(
        self,
        serializer: JsonSerializer | None = None,
        deserializer: JsonDeserializer | None = None,
    ) -> None:
        self._serializer = serializer or JsonSerializer()
        self._deserializer = deserializer or JsonDeserializer()

    @property
    def serializer(self) -> JsonSerializer:
        return self._serializer

    @property
    def deserializer(self) -> JsonDeserializer:
        return self._deserializer


#: Module-level default ``JsonSerde`` — use for vanilla encoder/decoder needs.
JSON_SERDE: Final[JsonSerde] = JsonSerde()


__all__ = ["JSON_SERDE", "JsonDeserializer", "JsonSerde", "JsonSerializer"]
