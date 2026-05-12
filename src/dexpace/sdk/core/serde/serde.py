""":class:`Serde`, :class:`Serializer`, :class:`Deserializer` Protocols."""
from __future__ import annotations

from typing import Any, BinaryIO, Protocol, runtime_checkable


@runtime_checkable
class Serializer(Protocol):
    """Format-agnostic serialization strategy.

    The three overloads cover the common allocation profiles: produce a fresh
    string, produce a fresh ``bytes``, or stream into a caller-owned
    ``BinaryIO``. Stream overloads do **not** close their targets — the caller
    retains ownership.
    """

    def serialize(self, value: Any) -> str: ...

    def serialize_to_bytes(self, value: Any) -> bytes: ...

    def serialize_to_stream(self, value: Any, stream: BinaryIO) -> None: ...


@runtime_checkable
class Deserializer(Protocol):
    """Format-agnostic deserialization strategy.

    The stream overload owns reading to EOF but does **not** close the stream
    — the caller retains ownership.
    """

    def deserialize(self, value: str) -> Any: ...

    def deserialize_bytes(self, value: bytes) -> Any: ...

    def deserialize_stream(self, stream: BinaryIO) -> Any: ...


@runtime_checkable
class Serde(Protocol):
    """Bundle of serialization / deserialization strategies for a single wire format.

    Acts as a single injection point: components that need to round-trip values
    pull a :class:`Serde` rather than separate serializer and deserializer
    references, which keeps the dependency surface flat and makes it easy to
    swap formats at the edge of the SDK.
    """

    @property
    def serializer(self) -> Serializer: ...

    @property
    def deserializer(self) -> Deserializer: ...


__all__ = ["Deserializer", "Serde", "Serializer"]
