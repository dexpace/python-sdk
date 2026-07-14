# Writing a serde

A `Serde` bundles the two directions of one wire format — encode a Python value
to bytes, decode bytes back to a value — behind a single injection point.
`core` ships `JSON_SERDE`; you implement your own when a service speaks a
different format (CBOR, MessagePack, form-encoded, a bespoke framing) and you
want the codec, response handlers, and request-body helpers to round-trip
through it unchanged.

The serde SPI is a pure structural seam: it carries no dedicated requirement-ID
in the spec index, so its checklist is the Protocol's own contract, and its
certification is the round-trip conformance suite rather than a marked
requirement. The contract is small and strict, and every clause below is
exercised by that suite.

## The three Protocols

```python
from __future__ import annotations

from typing import Any, BinaryIO

from dexpace.sdk.core.serde import Deserializer, Serde, Serializer
```

- `Serializer` turns a value into wire bytes, in three allocation profiles:
  `serialize(value) -> str`, `serialize_to_bytes(value) -> bytes`, and
  `serialize_to_stream(value, stream) -> None`.
- `Deserializer` is the inverse: `deserialize(str)`, `deserialize_bytes(bytes)`,
  and `deserialize_stream(stream)`.
- `Serde` exposes the pair plus the format's media type: the `serializer`,
  `deserializer`, and `media_type` properties.

All three are `runtime_checkable` Protocols; implement them structurally.

## A worked implementation

```python
from __future__ import annotations

import json
from typing import Any, BinaryIO

from dexpace.sdk.core.http.common import MediaType


class JsonSerializer:
    def serialize(self, value: Any) -> str:
        return json.dumps(value)

    def serialize_to_bytes(self, value: Any) -> bytes:
        return json.dumps(value).encode("utf-8")

    def serialize_to_stream(self, value: Any, stream: BinaryIO) -> None:
        stream.write(self.serialize_to_bytes(value))  # does NOT close the stream


class JsonDeserializer:
    def deserialize(self, value: str) -> Any:
        return json.loads(value)

    def deserialize_bytes(self, value: bytes) -> Any:
        return json.loads(value)

    def deserialize_stream(self, stream: BinaryIO) -> Any:
        return json.loads(stream.read())  # reads to EOF, does NOT close


class JsonSerde:
    def __init__(self) -> None:
        self._serializer = JsonSerializer()
        self._deserializer = JsonDeserializer()

    @property
    def serializer(self) -> JsonSerializer:
        return self._serializer

    @property
    def deserializer(self) -> JsonDeserializer:
        return self._deserializer

    @property
    def media_type(self) -> MediaType:
        return MediaType.parse("application/json")
```

## Contract checklist

A correct serde satisfies every clause below. These are the Protocol's own
contract, and the conformance suite enforces each one:

- **Round-trip fidelity.** For every value the format can represent,
  `deserialize_bytes(serialize_to_bytes(v)) == v`, and the same holds for the
  string and stream profiles. The three profiles agree with one another —
  streaming a value and byte-serialising it produce the same wire content.
- **Stream ownership stays with the caller.** `serialize_to_stream` and
  `deserialize_stream` read/write their target but **must not close it**. The
  caller owns the stream's lifetime. `deserialize_stream` reads to EOF.
- **The serde declares its own `media_type`.** The SPI supplies no default, so a
  request assembling a `Content-Type` reads the wire format off the serde
  instead of assuming JSON. Return the media type your bytes actually carry.
- **Failures raise the serde taxonomy.** Wire-syntax and shape errors surface as
  `SerializationError` (encode) or `DeserializationError` (decode); both are
  `SerdeError` and `ValueError`, so existing `except ValueError` handlers keep
  working. Do not swallow an encode/decode failure into a partial result. See
  [`errors.md`](errors.md).
- **No hidden global state.** A serde instance is reused concurrently by the
  codec and the response handlers, so keep it stateless (or internally
  thread-safe).

Your serde plugs into the rest of the stack by construction: pass it to
`DecodeSuccess(errors, serde=my_serde)`, to `ServiceCore(serde=my_serde)`, or
to the request-body helpers, and every decode/encode hop routes through it.

## Certifying your serde

The built-in serdes are held to a structural conformance suite that checks the
Protocol shape and the round-trip laws above. Point it at your implementation
(or add a parametrized case) and run it:

```bash
pytest packages/dexpace-sdk-core/tests/serde/test_serde_conformance.py
```

Run the wider serde suite (`packages/dexpace-sdk-core/tests/serde/`) to cover
the codec, `Tristate` folding, and response-handler decode paths your serde
feeds.
