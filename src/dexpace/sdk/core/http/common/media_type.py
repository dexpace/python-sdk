"""RFC 7231 media type model."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class MediaType:
    """An HTTP media type (RFC 7231 §3.1.1.1).

    Immutable and hashable. ``parameters`` is stored as a tuple of sorted
    ``(key, value)`` pairs so two equivalent media types compare equal and hash
    equally regardless of construction order. Construct via :meth:`of` or
    :meth:`parse` rather than the dataclass constructor directly so the type,
    subtype, and parameter keys are normalised to lower case.
    """

    type: str
    subtype: str
    parameters: tuple[tuple[str, str], ...] = ()

    @property
    def full_type(self) -> str:
        """``type/subtype`` form, parameters excluded."""
        return f"{self.type}/{self.subtype}"

    @property
    def charset(self) -> str | None:
        """The ``charset`` parameter, or ``None`` if absent."""
        for key, value in self.parameters:
            if key == "charset":
                return value
        return None

    def includes(self, other: MediaType) -> bool:
        """True when this media type pattern matches ``other``.

        Treats ``*`` in either the type or subtype position as a wildcard.
        Parameters are ignored.
        """
        type_matches = self.type == "*" or self.type == other.type
        subtype_matches = self.subtype == "*" or self.subtype == other.subtype
        return type_matches and subtype_matches

    def __str__(self) -> str:
        if not self.parameters:
            return f"{self.type}/{self.subtype}"
        formatted = ";".join(f"{key}={value}" for key, value in self.parameters)
        return f"{self.type}/{self.subtype};{formatted}"

    @classmethod
    def of(
        cls,
        type: str,
        subtype: str,
        parameters: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct a media type from explicit parts.

        Validates that ``type`` and ``subtype`` are non-blank and that a
        wildcard ``type`` is only paired with a wildcard ``subtype``.

        Raises:
            ValueError: if validation fails.
        """
        if not type or not type.strip():
            raise ValueError("type must not be blank")
        if not subtype or not subtype.strip():
            raise ValueError("subtype must not be blank")
        normalized_type = type.lower()
        normalized_subtype = subtype.lower()
        if normalized_type == "*" and normalized_subtype != "*":
            raise ValueError(
                f"Invalid media type: type=*, subtype={normalized_subtype}"
            )
        if parameters:
            params = tuple(
                sorted((k.lower(), v.lower()) for k, v in parameters.items())
            )
        else:
            params = ()
        return cls(normalized_type, normalized_subtype, params)

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a wire-form media type string (e.g. ``application/json;charset=utf-8``).

        Raises:
            ValueError: if ``value`` cannot be parsed.
        """
        if not value or not value.strip():
            raise ValueError("media type must not be blank")
        segments = [segment.strip() for segment in value.split(";")]
        mime = segments[0]
        slash = mime.find("/")
        if slash <= 0 or slash == len(mime) - 1:
            raise ValueError(f"Invalid media type: {value!r}")
        type_ = mime[:slash].strip()
        subtype = mime[slash + 1 :].strip()
        parameters_map: dict[str, str] = {}
        for segment in segments[1:]:
            if not segment:
                continue
            # Split on the first ``=`` only — parameter values may legitimately
            # contain additional ``=`` characters (e.g. multipart boundaries).
            eq = segment.find("=")
            if eq < 0:
                raise ValueError(f"Invalid parameter: {segment!r}")
            key = segment[:eq].strip()
            param_value = segment[eq + 1 :].strip()
            if not key or not param_value:
                raise ValueError(f"Invalid parameter: {segment!r}")
            parameters_map[key] = param_value
        return cls.of(type_, subtype, parameters_map)


__all__ = ["MediaType"]
