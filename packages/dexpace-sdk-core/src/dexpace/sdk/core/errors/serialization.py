# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Serialisation / deserialisation exceptions.

``SerdeError`` is the root of the serde failure taxonomy; the directional
``SerializationError`` (encode side) and ``DeserializationError`` (decode side)
subtypes let callers tell which way a round-trip failed while still catching the
family with a single ``except SerdeError``.

All three inherit from ``ValueError`` so existing ``except ValueError`` handlers
continue to work, and from ``SdkError`` so callers that catch the SDK root see
them. Like every Python exception they are unchecked — nothing forces a caller
to catch them.
"""

from __future__ import annotations

from .base import SdkError


class SerdeError(SdkError, ValueError):
    """Root of the serde failure taxonomy.

    Raised (via a directional subtype) when a value cannot cross the serde
    boundary in either direction. Catch this to handle any serde failure
    regardless of direction; catch a subtype to distinguish encode from decode.
    """


class SerializationError(SerdeError):
    """A value could not be serialised to the target format."""


class DeserializationError(SerdeError):
    """A payload could not be deserialised."""


__all__ = ["DeserializationError", "SerdeError", "SerializationError"]
