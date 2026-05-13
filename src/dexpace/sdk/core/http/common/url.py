"""Immutable URL value objects with `urllib.parse` interop centralised."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Self
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

type _QueryValue = str | Iterable[str]
type _QueryEntries = Mapping[str, _QueryValue] | Iterable[tuple[str, _QueryValue]]


class QueryParams:
    """Immutable, multi-valued query parameters.

    Mirrors ``Headers`` in shape: each name maps to an ordered tuple of
    string values, and ``with_*`` / ``without`` return new instances rather
    than mutating. Insertion order of distinct names is preserved.

    Unlike ``Headers``, names are case-sensitive — RFC 3986 §3.4 makes
    case-sensitivity URL-scheme-defined and the HTTP/HTTPS schemes do not
    canonicalise. Compare names exactly as written.
    """

    __slots__ = ("_data", "_hash")

    _data: tuple[tuple[str, tuple[str, ...]], ...]
    _hash: int | None

    def __init__(self, entries: _QueryEntries | None = None) -> None:
        data: dict[str, tuple[str, ...]] = {}
        if entries is not None:
            items: Iterable[tuple[str, _QueryValue]] = (
                entries.items() if isinstance(entries, Mapping) else entries
            )
            for name, value in items:
                existing = data.get(name, ())
                if isinstance(value, str):
                    data[name] = (*existing, value)
                else:
                    data[name] = (*existing, *value)
        object.__setattr__(self, "_data", tuple(data.items()))
        object.__setattr__(self, "_hash", None)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def get(self, name: str, default: str | None = None) -> str | None:
        """First value for ``name`` or ``default`` if absent."""
        for key, values in self._data:
            if key == name:
                return values[0] if values else default
        return default

    def values(self, name: str) -> tuple[str, ...]:
        """Every value for ``name`` (empty tuple if absent)."""
        for key, values in self._data:
            if key == name:
                return values
        return ()

    def __getitem__(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return any(key == name for key, _ in self._data)

    def __iter__(self) -> Iterator[str]:
        for key, _ in self._data:
            yield key

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """The underlying ``(name, values)`` tuples in insertion order."""
        return self._data

    def flatten(self) -> tuple[tuple[str, str], ...]:
        """Flat ``[(name, value), …]`` tuple — one entry per value, in order."""
        return tuple((key, value) for key, values in self._data for value in values)

    def with_added(self, name: str, value: str) -> Self:
        entries: list[tuple[str, tuple[str, ...]]] = []
        appended = False
        for key, values in self._data:
            if key == name:
                entries.append((key, (*values, value)))
                appended = True
            else:
                entries.append((key, values))
        if not appended:
            entries.append((name, (value,)))
        return _construct_query(type(self), tuple(entries))

    def with_set(self, name: str, *values: str) -> Self:
        entries: list[tuple[str, tuple[str, ...]]] = []
        replaced = False
        for key, existing in self._data:
            if key == name:
                if not replaced:
                    entries.append((key, tuple(values)))
                    replaced = True
            else:
                entries.append((key, existing))
        if not replaced:
            entries.append((name, tuple(values)))
        return _construct_query(type(self), tuple(entries))

    def without(self, name: str) -> Self:
        entries = tuple((key, values) for key, values in self._data if key != name)
        if len(entries) == len(self._data):
            return self
        return _construct_query(type(self), entries)

    def encode(self) -> str:
        """Serialise to ``application/x-www-form-urlencoded`` form (no leading ``?``)."""
        return urlencode(self.flatten(), doseq=False, quote_via=quote)

    def __str__(self) -> str:
        return self.encode()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, QueryParams):
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
        return f"QueryParams({{{parts}}})"

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse a ``foo=1&bar=2`` query string (leading ``?`` is tolerated).

        ``parse_qsl`` already percent-decodes keys and values; we do not
        call ``unquote`` again to avoid double-decoding ``%2520`` → space.
        """
        if raw.startswith("?"):
            raw = raw[1:]
        if not raw:
            return _construct_query(cls, ())
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
        return cls(pairs)

    @classmethod
    def empty(cls) -> QueryParams:
        return _EMPTY_QUERY


def _construct_query[Q: QueryParams](
    cls: type[Q],
    data: tuple[tuple[str, tuple[str, ...]], ...],
) -> Q:
    instance = cls.__new__(cls)
    object.__setattr__(instance, "_data", data)
    object.__setattr__(instance, "_hash", None)
    return instance


_EMPTY_QUERY = QueryParams()


@dataclass(frozen=True, slots=True)
class Url:
    """Immutable parsed URL.

    Construct via ``Url.parse`` for a wire-form string; use
    ``dataclasses.replace`` or the ``with_*`` helpers for derivative
    instances. ``__str__`` serialises back to wire form via
    ``urllib.parse.urlunsplit``.

    The ``query`` component is a ``QueryParams`` so caller code can read and
    mutate query parameters without reparsing.

    Attributes:
        scheme: URL scheme (e.g. ``https``).
        host: Hostname without port or userinfo.
        path: Path component (may be empty).
        port: Optional explicit port.
        query: Query parameters as a ``QueryParams``.
        fragment: Fragment after ``#``.
        userinfo: Optional ``user[:password]`` segment.
    """

    scheme: str
    host: str
    path: str = ""
    port: int | None = None
    query: QueryParams = field(default_factory=QueryParams)
    fragment: str = ""
    userinfo: str | None = None

    def authority(self, *, with_userinfo: bool) -> str:
        """``[userinfo@]host[:port]`` — the netloc component.

        Args:
            with_userinfo: When True, includes ``user[:password]@`` in the
                result. Default callers (``__str__``) pass False to avoid
                leaking credentials into logs.
        """
        parts: list[str] = []
        if with_userinfo and self.userinfo is not None:
            parts.append(self.userinfo + "@")
        parts.append(self.host)
        if self.port is not None:
            parts.append(f":{self.port}")
        return "".join(parts)

    def __str__(self) -> str:
        query_string = self.query.encode() if len(self.query) else ""
        return urlunsplit(
            (
                self.scheme,
                self.authority(with_userinfo=False),
                self.path,
                query_string,
                self.fragment,
            )
        )

    def wire_form(self) -> str:
        """Serialise to wire form including ``userinfo`` if present.

        Use this when building a literal request line that must carry
        credentials; default ``str(url)`` redacts userinfo to avoid
        accidental leakage through logging.
        """
        query_string = self.query.encode() if len(self.query) else ""
        return urlunsplit(
            (
                self.scheme,
                self.authority(with_userinfo=True),
                self.path,
                query_string,
                self.fragment,
            )
        )

    def __repr__(self) -> str:
        userinfo = "[REDACTED]" if self.userinfo else None
        return (
            f"Url(scheme={self.scheme!r}, host={self.host!r}, "
            f"path={self.path!r}, port={self.port!r}, "
            f"query={self.query!r}, fragment={self.fragment!r}, "
            f"userinfo={userinfo!r})"
        )

    def with_path(self, path: str) -> Self:
        return replace(self, path=path)

    def with_query(self, query: QueryParams) -> Self:
        return replace(self, query=query)

    def with_fragment(self, fragment: str) -> Self:
        return replace(self, fragment=fragment)

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse a wire-form URL string.

        Args:
            raw: Full URL including scheme and host.

        Returns:
            The parsed URL.

        Raises:
            ValueError: If ``raw`` is empty or lacks a scheme or host.
        """
        if not raw:
            raise ValueError("URL must not be empty")
        split = urlsplit(raw)
        if not split.scheme:
            raise ValueError(f"URL missing scheme: {raw!r}")
        if not split.hostname:
            raise ValueError(f"URL missing host: {raw!r}")
        userinfo: str | None = None
        if split.username is not None:
            userinfo = split.username
            if split.password is not None:
                userinfo = f"{split.username}:{split.password}"
        return cls(
            scheme=split.scheme,
            host=split.hostname,
            port=split.port,
            path=split.path,
            query=QueryParams.parse(split.query),
            fragment=split.fragment,
            userinfo=userinfo,
        )


__all__ = ["QueryParams", "Url"]
