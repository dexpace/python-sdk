"""Immutable, case-insensitive, multi-valued HTTP headers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Final, Self

from .http_header_name import HttpHeaderName

# RFC 7230 token: 1*tchar where tchar is the set of ASCII characters allowed
# in header field names. We match against the lower-cased name because header
# names are case-insensitive and stored in lower form.
_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[!#$%&'*+\-.^_`|~0-9a-z]+$")

type _Name = str | HttpHeaderName
type _HeaderValue = str | Iterable[str]
# ``Mapping[str, …]`` rather than ``Mapping[_Name, …]`` because ``dict[str, T]``
# is not a subtype of ``Mapping[str | X, T]`` (key invariance). Callers
# wanting :class:`HttpHeaderName` keys can pass the iterable-of-pairs form.
type _Entries = Mapping[str, _HeaderValue] | Iterable[tuple[_Name, _HeaderValue]]


class Headers:
    """Immutable, case-insensitive, multi-valued HTTP headers.

    Header names are normalised to lower case at storage time so lookup,
    membership, and equality are all case-insensitive. Insertion order of
    distinct names is preserved.

    Multi-value semantics: :meth:`with_added` appends to the values list for
    a name; :meth:`with_set` replaces the entire list. This matches the HTTP
    requirement that some headers (``Set-Cookie``, ``WWW-Authenticate``,
    ``Via``) may legitimately repeat.

    Instances are immutable and freely shareable across threads.
    """

    __slots__ = ("_data", "_hash")

    _data: tuple[tuple[str, tuple[str, ...]], ...]
    _hash: int | None

    def __init__(self, entries: _Entries | None = None) -> None:
        data: dict[str, tuple[str, ...]] = {}
        if entries is not None:
            items: Iterable[tuple[_Name, _HeaderValue]] = (
                entries.items() if isinstance(entries, Mapping) else entries
            )
            for name, value in items:
                key = _normalize(name)
                existing = data.get(key, ())
                new_values: tuple[str, ...] = (value,) if isinstance(value, str) else tuple(value)
                for v in new_values:
                    if "\r" in v or "\n" in v or "\0" in v:
                        raise ValueError(
                            f"invalid header value for {key!r}: contains control characters"
                        )
                data[key] = (*existing, *new_values)
        object.__setattr__(self, "_data", tuple(data.items()))
        object.__setattr__(self, "_hash", None)

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
            merged[key] = merged.get(key, ()) + values
        return _construct(type(self), tuple(merged.items()))

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
        parts = ", ".join(f"{key!r}: {list(values)!r}" for key, values in self._data)
        return f"Headers({{{parts}}})"

    @classmethod
    def empty(cls) -> Headers:
        """Return the shared empty :class:`Headers` instance."""
        return _EMPTY


def _normalize(name: _Name) -> str:
    # HttpHeaderName already holds the lower-case form; for raw strings we
    # canonicalise here. RFC 7230: header names are ASCII so `.lower()` is
    # sufficient and casefold() is unnecessary overhead on the hot path.
    if isinstance(name, HttpHeaderName):
        return name.value
    lowered = name.lower()
    if not _TOKEN.match(lowered):
        raise ValueError(f"invalid header name: {name!r}")
    return lowered


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
