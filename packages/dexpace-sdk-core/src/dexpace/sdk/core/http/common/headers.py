# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Immutable, case-insensitive, multi-valued HTTP headers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Final, Self

from .http_header_name import HttpHeaderName

# RFC 7230 token: 1*tchar where tchar is the set of ASCII characters allowed
# in header field names. We match against the *original* (pre-fold) name so
# non-ASCII input is rejected outright rather than silently ASCII-folded — e.g.
# U+212A KELVIN SIGN lower-cases to "k" and U+0130 (dotted capital I) folds to
# "i" plus a combining mark, both of which would otherwise slip past a
# lower-cased token check. Matching the original keeps case-folding ASCII-only
# and locale-invariant (see ``_normalize``).
_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Any character outside RFC 7230 ``field-content`` (HTAB, SP, and VCHAR
# 0x21-0x7E). Used only on the outbound-strict path: it also matches obs-text
# (0x80-0xFF), which received (inbound) messages must tolerate but generated
# (outbound) ones must not emit.
_NON_FIELD_CONTENT: Final[re.Pattern[str]] = re.compile(r"[^\t\x20-\x7e]")

# Headers whose values should be redacted in repr() to avoid accidental
# credential leakage through logging or exception formatting.
_SENSITIVE: Final[frozenset[str]] = frozenset(
    {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
)

type _Name = str | HttpHeaderName
type _HeaderValue = str | Iterable[str]
# ``Mapping[str, …]`` rather than ``Mapping[_Name, …]`` because ``dict[str, T]``
# is not a subtype of ``Mapping[str | X, T]`` (key invariance). Callers
# wanting `HttpHeaderName` keys can pass the iterable-of-pairs form.
type _Entries = Mapping[str, _HeaderValue] | Iterable[tuple[_Name, _HeaderValue]]


class Headers:
    """Immutable, case-insensitive, multi-valued HTTP headers.

    Header names are normalised to lower case at storage time so lookup,
    membership, and equality are all case-insensitive. Insertion order of
    distinct names is preserved.

    Multi-value semantics: `with_added` appends to the values list for
    a name; `with_set` replaces the entire list. This matches the HTTP
    requirement that some headers (``Set-Cookie``, ``WWW-Authenticate``,
    ``Via``) may legitimately repeat.

    Validation has two strictness levels, both of which always reject the
    CR / LF / NUL bytes that enable header injection:

    * The default constructor is **inbound-lenient**: it tolerates obs-text
      (bytes 0x80-0xFF), so response headers decoded as Latin-1 by a transport
      round-trip unchanged, per RFC 7230's rule that received messages accept
      obs-text.
    * `outbound` is **outbound-strict**: it additionally rejects obs-text and
      other non-``field-content`` bytes, so generated request headers never
      emit characters RFC 7230 forbids on the wire.

    Instances are immutable and freely shareable across threads.
    """

    __slots__ = ("_data", "_hash")

    _data: tuple[tuple[str, tuple[str, ...]], ...]
    _hash: int | None

    def __init__(self, entries: _Entries | None = None) -> None:
        object.__setattr__(self, "_data", _build(entries, strict=False))
        object.__setattr__(self, "_hash", None)

    @classmethod
    def outbound(cls, entries: _Entries | None = None) -> Self:
        """Build headers for an outbound request under strict RFC 7230 rules.

        Identical to the default constructor except that header values are
        additionally rejected if they contain obs-text (bytes 0x80-0xFF) or any
        other character outside ``field-content`` (HTAB, SP, and visible ASCII).
        Use this when assembling headers the SDK will *send*, so a spec-clean
        request never carries bytes forbidden on the wire.

        Args:
            entries: Initial headers as a mapping or an iterable of
                ``(name, value | values)`` pairs.

        Returns:
            A new `Headers` populated from ``entries``.

        Raises:
            ValueError: If a name is not a valid token, or a value contains
                CR / LF / NUL, obs-text, or other non-``field-content`` bytes.
        """
        return _construct(cls, _build(entries, strict=True))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def get(self, name: _Name, default: str | None = None) -> str | None:
        """Return the first value for ``name``, or ``default`` if absent."""
        target = _normalize(name)
        for key, values in self._data:
            if key == target:
                return values[0] if values else default
        return default

    def values(self, name: _Name) -> tuple[str, ...]:
        """Return every value for ``name`` as a tuple; empty if absent."""
        target = _normalize(name)
        for key, values in self._data:
            if key == target:
                return values
        return ()

    def __getitem__(self, name: _Name) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        if isinstance(name, HttpHeaderName):
            target = name.value
        elif isinstance(name, str):
            target = _normalize(name)
        else:
            return False
        return any(key == target for key, _ in self._data)

    def __iter__(self) -> Iterator[str]:
        for key, _ in self._data:
            yield key

    def __len__(self) -> int:
        return len(self._data)

    def names(self) -> tuple[str, ...]:
        """Return the tuple of header names (lower-cased)."""
        return tuple(key for key, _ in self._data)

    def items(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return the underlying ``(name, values)`` tuples in insertion order."""
        return self._data

    def with_added(self, name: _Name, value: str) -> Self:
        """Return a new ``Headers`` with ``value`` appended to ``name``'s list."""
        target = _normalize(name)
        _check_value(target, value)
        entries: list[tuple[str, tuple[str, ...]]] = []
        appended = False
        for key, values in self._data:
            if key == target:
                entries.append((key, (*values, value)))
                appended = True
            else:
                entries.append((key, values))
        if not appended:
            entries.append((target, (value,)))
        return _construct(type(self), tuple(entries))

    def with_set(self, name: _Name, *values: str) -> Self:
        """Return a new ``Headers`` with ``name`` set to exactly ``values``.

        If no values are provided, the header is removed.
        """
        if not values:
            return self.without(name)
        target = _normalize(name)
        for value in values:
            _check_value(target, value)
        entries: list[tuple[str, tuple[str, ...]]] = []
        replaced = False
        for key, existing in self._data:
            if key == target:
                if not replaced:
                    entries.append((key, tuple(values)))
                    replaced = True
                # else: drop any later duplicates
            else:
                entries.append((key, existing))
        if not replaced:
            entries.append((target, tuple(values)))
        return _construct(type(self), tuple(entries))

    def without(self, name: _Name) -> Self:
        """Return a new ``Headers`` with ``name`` removed (case-insensitive)."""
        target = _normalize(name)
        entries = tuple((key, values) for key, values in self._data if key != target)
        if len(entries) == len(self._data):
            return self
        return _construct(type(self), entries)

    def with_merged(self, other: Headers) -> Self:
        """Append every entry from ``other`` to this headers."""
        merged: dict[str, tuple[str, ...]] = dict(self._data)
        for key, values in other._data:
            for value in values:
                _check_value(key, value)
            merged[key] = merged.get(key, ()) + values
        return _construct(type(self), tuple(merged.items()))

    def __or__(self, other: Headers) -> Self:
        """Return ``self.with_merged(other)`` — enables ``a | b`` syntax.

        Args:
            other: Another `Headers` whose entries are appended.

        Returns:
            A new `Headers` combining both sides; ``self``'s values for
            each name appear before ``other``'s.
        """
        if not isinstance(other, Headers):
            return NotImplemented
        return self.with_merged(other)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Headers):
            return NotImplemented
        return self._data == other._data

    def __hash__(self) -> int:
        cached = self._hash
        if cached is None:
            cached = hash(self._data)
            object.__setattr__(self, "_hash", cached)
        return cached

    def __repr__(self) -> str:
        rendered: list[str] = []
        for key, values in self._data:
            if key in _SENSITIVE:
                shown: list[str] = ["[REDACTED]"] * len(values)
            else:
                shown = list(values)
            rendered.append(f"{key!r}: {shown!r}")
        return f"Headers({{{', '.join(rendered)}}})"

    @classmethod
    def empty(cls) -> Headers:
        """Return the shared empty `Headers` instance."""
        return _EMPTY


def _build(entries: _Entries | None, *, strict: bool) -> tuple[tuple[str, tuple[str, ...]], ...]:
    # Shared construction path for the lenient (``__init__``) and strict
    # (``outbound``) entry points; ``strict`` selects the value-validation rule.
    data: dict[str, tuple[str, ...]] = {}
    if entries is not None:
        items: Iterable[tuple[_Name, _HeaderValue]] = (
            entries.items() if isinstance(entries, Mapping) else entries
        )
        for name, value in items:
            key = _normalize(name)
            new_values: tuple[str, ...] = (value,) if isinstance(value, str) else tuple(value)
            for v in new_values:
                _check_value(key, v, strict=strict)
            data[key] = (*data.get(key, ()), *new_values)
    return tuple(data.items())


def _normalize(name: _Name) -> str:
    # HttpHeaderName already holds the trusted lower-case form, so skip the
    # token check and the ``.lower()`` call entirely on that hot path.
    if isinstance(name, HttpHeaderName):
        return name.value
    # Validate the *original* name: the token set is ASCII-only, so a non-ASCII
    # name fails here instead of being ASCII-folded by ``.lower()`` (see
    # ``_TOKEN``). Lower-casing a proven-ASCII string is locale-invariant — it
    # never triggers the Turkish dotless-i or Kelvin-sign special-casing traps.
    if not _TOKEN.match(name):
        raise ValueError(f"invalid header name: {name!r}")
    return name.lower()


def _check_value(key: str, value: str, *, strict: bool = False) -> None:
    # Reject CR, LF, and NUL on every path to prevent header injection /
    # response splitting. ``key`` is the already-normalised name, echoed via
    # ``!r`` so any control characters in it are escaped; the value is never
    # echoed, so a rejected secret cannot leak through the message.
    if "\r" in value or "\n" in value or "\0" in value:
        raise ValueError(f"invalid header value for {key!r}: contains control characters")
    # Outbound-strict additionally forbids obs-text and other non-field-content
    # bytes, which received (inbound) headers must instead tolerate.
    if strict and _NON_FIELD_CONTENT.search(value):
        raise ValueError(
            f"invalid header value for {key!r}: contains obs-text or non-ASCII control characters"
        )


def _construct[H: Headers](
    cls: type[H],
    data: tuple[tuple[str, tuple[str, ...]], ...],
) -> H:
    instance = cls.__new__(cls)
    object.__setattr__(instance, "_data", data)
    object.__setattr__(instance, "_hash", None)
    return instance


_EMPTY = Headers()


__all__ = ["Headers"]
