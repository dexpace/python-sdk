# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Format-agnostic serialization / deserialization contracts and JSON impl."""

from __future__ import annotations

from .codec import (
    ALIAS_KEY,
    DISCRIMINATOR_KEY,
    REGISTRY_KEY,
    Codec,
    CodecError,
    discriminated,
    field_alias,
    variant,
)
from .json_serde import JSON_SERDE, JsonDeserializer, JsonSerde, JsonSerializer
from .serde import Deserializer, Serde, Serializer
from .tristate import (
    ABSENT,
    NULL,
    Present,
    Tristate,
    fold,
    is_absent,
    is_null,
    is_present,
    of_optional,
    present,
)

__all__ = [
    "ABSENT",
    "ALIAS_KEY",
    "DISCRIMINATOR_KEY",
    "JSON_SERDE",
    "NULL",
    "REGISTRY_KEY",
    "Codec",
    "CodecError",
    "Deserializer",
    "JsonDeserializer",
    "JsonSerde",
    "JsonSerializer",
    "Present",
    "Serde",
    "Serializer",
    "Tristate",
    "discriminated",
    "field_alias",
    "fold",
    "is_absent",
    "is_null",
    "is_present",
    "of_optional",
    "present",
    "variant",
]
