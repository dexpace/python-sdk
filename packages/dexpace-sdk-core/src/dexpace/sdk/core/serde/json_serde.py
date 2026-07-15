# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Stdlib-backed ``Serde`` for JSON payloads."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, BinaryIO, Final

from ..errors import DeserializationError, SerializationError
from ..http.common import common_media_types

if TYPE_CHECKING:
    from ..http.common.media_type import MediaType


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


def _build_encoder(
    encoder_cls: type[json.JSONEncoder],
    default: Callable[[Any], Any] | None,
    sort_keys: bool,
    allow_nan: bool,
) -> json.JSONEncoder:
    """Build an encoder, chaining the built-in ``default`` to a user one.

    Called once per ``JsonSerializer``. When a custom ``default`` is supplied,
    a small subclass routes its callable behind the encoder class so the
    built-in datetime / date / time / bytes handling still runs first.

    Args:
        encoder_cls: The base ``json.JSONEncoder`` subclass.
        default: Optional user fallback for unencodable values.
        sort_keys: Whether to emit object keys in sorted order.
        allow_nan: Whether to permit ``NaN`` / ``Infinity`` tokens.

    Returns:
        A configured, reusable encoder instance.
    """
    kwargs: dict[str, Any] = {
        "sort_keys": sort_keys,
        "allow_nan": allow_nan,
        "separators": (",", ":"),
    }
    if default is None:
        return encoder_cls(**kwargs)
    fallback: Callable[[Any], Any] = default

    class _ChainedEncoder(encoder_cls):  # type: ignore[valid-type, misc]
        def default(self, o: Any) -> Any:
            try:
                return super().default(o)
            except TypeError:
                return fallback(o)

    return _ChainedEncoder(**kwargs)


class JsonSerializer:
    """Serialise Python values into JSON strings / bytes / streams.

    The encoder is built once at construction and reused for every
    ``serialize`` call. A ``json.JSONEncoder`` instance is stateless across
    ``encode`` calls, so sharing it costs nothing and avoids re-deriving an
    encoder class on every serialize when a custom ``default`` is configured.
    """

    __slots__ = ("_encoder",)

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
        self._encoder = _build_encoder(encoder_cls, default, sort_keys, allow_nan)

    def serialize(self, value: Any) -> str:
        """Serialise ``value`` to a JSON string.

        ``json`` treats ``cls=`` and ``default=`` as mutually exclusive: passing
        both makes the encoder class's ``default`` method unreachable. To keep
        the built-in datetime / date / time / bytes handling alongside a
        caller-supplied ``default``, the user callable is chained behind the
        encoder class instead — the encoder's checks run first and the user
        fallback only sees types the encoder rejects.

        Raises:
            SerializationError: If encoding fails.
        """
        try:
            return self._encoder.encode(value)
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

    def serialize_into(self, value: Any, buffer: bytearray | memoryview, offset: int = 0) -> int:
        """Serialise ``value`` into a caller-owned buffer at ``offset``.

        Encodes to bytes first, so an unserialisable value surfaces as
        ``SerializationError`` before any byte is written. Overflowing the
        buffer is a caller/range error, not a serde-shape problem, so it raises
        ``ValueError`` rather than a ``SerdeError``. The buffer is never grown.

        Args:
            value: The value to serialise.
            buffer: A writable buffer (``bytearray`` or a writable
                ``memoryview``) to receive the encoded bytes.
            offset: Index at which to begin writing; must be non-negative.

        Returns:
            The number of bytes written.

        Raises:
            SerializationError: If ``value`` cannot be encoded.
            ValueError: If ``offset`` is negative, or the encoded bytes do not
                fit in ``buffer`` starting at ``offset``.
        """
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        data = self.serialize_to_bytes(value)
        view = memoryview(buffer)
        end = offset + len(data)
        if end > len(view):
            raise ValueError(
                f"buffer too small: {len(data)} byte(s) at offset {offset} need "
                f"{end} byte(s), buffer holds {len(view)}",
            )
        view[offset:end] = data
        return len(data)


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
        media_type: The wire content type, ``application/json``.
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

    @property
    def media_type(self) -> MediaType:
        """The media type this bundle reads and writes: ``application/json``.

        Declared explicitly by the bundle rather than defaulted at the ``Serde``
        SPI boundary, so a consumer wiring a request's ``Content-Type`` reads it
        from the concrete serde instead of assuming JSON.
        """
        return common_media_types.APPLICATION_JSON


#: Module-level default ``JsonSerde`` — use for vanilla encoder/decoder needs.
JSON_SERDE: Final[JsonSerde] = JsonSerde()


__all__ = ["JSON_SERDE", "JsonDeserializer", "JsonSerde", "JsonSerializer"]
