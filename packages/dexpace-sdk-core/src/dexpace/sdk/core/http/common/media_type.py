# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""RFC 7231 media type model."""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

# RFC 7230 §3.2.6 — characters that are NOT valid in a ``token``. Any
# parameter value containing one of these (or a non-printable byte) must be
# rendered as a quoted-string. Space and HTAB are included because they
# terminate a token even though they are valid inside quoted-strings.
_TOKEN_SEPARATORS = frozenset('()<>@,;:\\"/[]?={} \t')


def _split_params(value: str) -> list[str]:
    """Split a media-type wire string on ``;`` outside quoted-strings.

    A naive ``value.split(";")`` mis-parses a quoted parameter value that
    legitimately contains a semicolon (legal inside an RFC 7230 §3.2.6
    quoted-string, e.g. an exotic multipart boundary). This walks the string
    with a small state machine that treats ``;`` as a separator only when it
    is not enclosed in double quotes, honouring the ``\\X`` quoted-pair escape
    so an escaped quote does not toggle the quoted state.

    Args:
        value: The full wire-form media-type string.

    Returns:
        The segments split on top-level ``;`` separators, in order. Surrounding
        whitespace is not stripped here — callers strip per segment.
    """
    segments: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    while i < len(value):
        ch = value[i]
        if in_quotes and ch == "\\" and i + 1 < len(value):
            current.append(ch)
            current.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ";" and not in_quotes:
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _unquote(s: str) -> str:
    """Decode a quoted-string parameter value per RFC 7230 §3.2.6.

    If ``s`` is wrapped in double quotes, the quotes are stripped and any
    ``\\X`` quoted-pair inside is replaced with ``X``. Values that are not
    quoted are returned unchanged.
    """
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    inner = s[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            out.append(inner[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _quote_if_needed(s: str) -> str:
    """Wrap ``s`` in a quoted-string when it is not a bare RFC 7230 token.

    A token consists of visible ASCII characters excluding the separator
    set. Any other character (space, ``;``, ``"``, control bytes, non-ASCII,
    etc.) forces quoting. Inside the quoted form ``"`` and ``\\`` are
    escaped with a leading backslash.
    """
    if s and all(ch.isascii() and ch.isprintable() and ch not in _TOKEN_SEPARATORS for ch in s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class MediaType:
    """An HTTP media type (RFC 7231 §3.1.1.1).

    Immutable and hashable. The normalised parts are held in unexported
    fields and read through the `type`, `subtype`, and `parameters`
    properties, so the invariant — lower-case type/subtype, parameters sorted
    with lower-cased keys — cannot be bypassed by holding raw state. `__post_init__`
    normalises on every construction path, so two equivalent media types
    compare equal and hash equally regardless of the order or case they were
    built with. Prefer the `of` or `parse` factories, which also validate.

    Attributes are exposed as read-only properties; assigning to them (or to
    the frozen backing fields) raises, matching the immutable-model contract.
    """

    _type: str
    _subtype: str
    _parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Normalise regardless of construction path (raw constructor included),
        # so the equality/hash invariant holds for every instance rather than
        # only those built through ``of``/``parse``. The dataclass is frozen,
        # so mutate the backing fields via ``object.__setattr__``. Parameter
        # *keys* are case-insensitive per RFC 7231 and are lower-cased, but
        # *values* are preserved verbatim — multipart boundaries and similar
        # tokens must round-trip exactly.
        object.__setattr__(self, "_type", self._type.lower())
        object.__setattr__(self, "_subtype", self._subtype.lower())
        normalized = tuple(sorted((key.lower(), value) for key, value in self._parameters))
        object.__setattr__(self, "_parameters", normalized)

    @property
    def type(self) -> str:
        """The lower-cased primary type (e.g. ``application``)."""
        return self._type

    @property
    def subtype(self) -> str:
        """The lower-cased subtype (e.g. ``json``)."""
        return self._subtype

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        """The parameters as sorted ``(lower-cased key, value)`` pairs."""
        return self._parameters

    @property
    def full_type(self) -> str:
        """``type/subtype`` form, parameters excluded."""
        return f"{self._type}/{self._subtype}"

    @property
    def charset(self) -> str | None:
        """The ``charset`` parameter as a known codec name, or ``None``.

        Returns ``None`` when the parameter is absent *or* names an encoding
        that the Python codec registry does not recognise. Degrading an
        unknown charset to ``None`` (rather than raising) lets callers fall
        back to a default encoding instead of failing to decode a body.
        """
        for key, value in self._parameters:
            if key == "charset":
                try:
                    codecs.lookup(value)
                except (LookupError, ValueError):
                    return None
                return value
        return None

    def includes(self, other: MediaType) -> bool:
        """True when this media type pattern matches ``other``.

        Treats ``*`` in either the type or subtype position as a wildcard.
        Parameters are ignored.
        """
        type_matches = self._type == "*" or self._type == other._type
        subtype_matches = self._subtype == "*" or self._subtype == other._subtype
        return type_matches and subtype_matches

    def __repr__(self) -> str:
        return (
            f"MediaType(type={self._type!r}, subtype={self._subtype!r}, "
            f"parameters={self._parameters!r})"
        )

    def __str__(self) -> str:
        if not self._parameters:
            return f"{self._type}/{self._subtype}"
        formatted = ";".join(f"{key}={_quote_if_needed(value)}" for key, value in self._parameters)
        return f"{self._type}/{self._subtype};{formatted}"

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
        # ``__post_init__`` lower-cases and sorts; validate the wildcard rule
        # against the same normalised form it will produce.
        if type.lower() == "*" and subtype.lower() != "*":
            raise ValueError(f"Invalid media type: type=*, subtype={subtype.lower()}")
        params = tuple(parameters.items()) if parameters else ()
        return cls(type, subtype, params)

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a wire-form media type string (e.g. ``application/json;charset=utf-8``).

        Raises:
            ValueError: if ``value`` cannot be parsed.
        """
        if not value or not value.strip():
            raise ValueError("media type must not be blank")
        segments = [segment.strip() for segment in _split_params(value)]
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
            parameters_map[key] = _unquote(param_value)
        return cls.of(type_, subtype, parameters_map)


__all__ = ["MediaType"]
