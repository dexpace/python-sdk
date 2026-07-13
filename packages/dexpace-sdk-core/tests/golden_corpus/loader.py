# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Loader API for the golden wire-exactness byte corpus.

Reads the checked-in raw byte fixtures under ``data/`` via
``importlib.resources`` — always in binary mode, never as text, so CR / LF /
CRLF SSE fixtures and percent-encoded query bytes survive untouched — and
exposes each vector as a frozen, slotted record.

Every fixture family is tagged with a requirement-ID-style label (``HTTP-29``,
``REDIR-12``, ``PAGE-21``, ``SSE-14``, ``RETRY-19``, ``CFG-29``) so a later
traceability tool can map vectors back to their spec requirements. Each
family's raw files plus its ``manifest.jsonl`` index live in a single
self-contained directory, so a future move into a standalone TCK distribution
(shipping the corpus as package data via ``importlib.resources``) is a clean
lift rather than a scatter-gather.

The loader carries no codec logic of its own: it materialises the fixture set
and the expectations the fixtures pin. The URL codec, redirect resolver,
pagination splicer, SSE parser, retry-pacing parser, and RFC 1123 date helpers
are implemented elsewhere and point their conformance checks at this corpus.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files
from typing import cast

if __package__ is None:  # pragma: no cover - always imported as a package
    raise RuntimeError("golden_corpus.loader must be imported as part of its package")

_ANCHOR: str = __package__
_DATA_SUBDIR: str = "data"

#: Every fixture family, in a stable order.
FAMILIES: tuple[str, ...] = (
    "query_codec",
    "redirect",
    "pagination_splice",
    "sse",
    "retry_pacing",
    "rfc1123_dates",
)

#: Family -> requirement-ID-style label, for the traceability tool.
FAMILY_TAGS: dict[str, str] = {
    "query_codec": "HTTP-29",
    "redirect": "REDIR-12",
    "pagination_splice": "PAGE-21",
    "sse": "SSE-14",
    "retry_pacing": "RETRY-19",
    "rfc1123_dates": "CFG-29",
}


# ----- record types -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryCodecVector:
    """One URL query codec vector (strict RFC 3986 render / lenient parse).

    Attributes:
        family: The owning family (``"query_codec"``).
        tag: Requirement-ID label (``"HTTP-29"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        params: Ordered ``(name, value)`` pairs as raw bytes.
        query: The wire query string bytes (no leading ``?``).
        render_matches: True when strict render of ``params`` equals ``query``.
        parse_matches: True when lenient parse of ``query`` equals ``params``.
    """

    family: str
    tag: str
    id: str
    description: str
    params: tuple[tuple[bytes, bytes], ...]
    query: bytes
    render_matches: bool
    parse_matches: bool

    @property
    def round_trippable(self) -> bool:
        """True when the vector renders and parses as exact inverses."""
        return self.render_matches and self.parse_matches


@dataclass(frozen=True, slots=True)
class RedirectVector:
    """One redirect ``Location`` resolution vector.

    Attributes:
        family: The owning family (``"redirect"``).
        tag: Requirement-ID label (``"REDIR-12"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        base: The base request URL bytes.
        location: The raw ``Location`` header bytes.
        resolved: The expected absolute resolved URL bytes.
    """

    family: str
    tag: str
    id: str
    description: str
    base: bytes
    location: bytes
    resolved: bytes


@dataclass(frozen=True, slots=True)
class PaginationSpliceVector:
    """One raw-query pagination splice vector.

    Attributes:
        family: The owning family (``"pagination_splice"``).
        tag: Requirement-ID label (``"PAGE-21"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        base: The base URL bytes (may already carry a raw query).
        param: The parameter name spliced in.
        value: The parameter value spliced in.
        spliced: The expected result URL bytes.
        untouched: Raw ``name=value`` substrings that must survive verbatim.
    """

    family: str
    tag: str
    id: str
    description: str
    base: bytes
    param: bytes
    value: bytes
    spliced: bytes
    untouched: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class SseExpectedEvent:
    """One expected parsed Server-Sent Event.

    Attributes:
        data: The (possibly multi-line) payload.
        event: The event name (``"message"`` by default).
        id: The sticky last-event-id, or None.
        retry: The reconnect hint in milliseconds, or None.
    """

    data: str
    event: str
    id: str | None
    retry: int | None


@dataclass(frozen=True, slots=True)
class SseVector:
    """One SSE stream vector.

    Attributes:
        family: The owning family (``"sse"``).
        tag: Requirement-ID label (``"SSE-14"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        stream: The raw stream bytes, with literal CR / LF / CRLF terminators.
        terminator: One of ``"LF"``, ``"CR"``, ``"CRLF"``.
        events: The expected sequence of parsed events.
    """

    family: str
    tag: str
    id: str
    description: str
    stream: bytes
    terminator: str
    events: tuple[SseExpectedEvent, ...]


@dataclass(frozen=True, slots=True)
class RetryPacingVector:
    """One ``Retry-After`` pacing vector.

    Attributes:
        family: The owning family (``"retry_pacing"``).
        tag: Requirement-ID label (``"RETRY-19"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        raw: The raw ``Retry-After`` token bytes.
        kind: One of ``"delta"``, ``"http_date"``, ``"reject"``.
        seconds: Expected parsed seconds after past-flooring but before any
            clamp; None for a rejected (malformed) value.
        now: Epoch reference used to convert an HTTP-date to a delta, or None.
        clamp_max: The clamp ceiling in seconds when the vector exercises the
            365-day clamp, else None.
        clamped: Expected seconds after applying ``clamp_max``, else None.
        float_accepts: Whether Python's ``float()`` accepts the raw token —
            pins the divergence for ``nan`` / ``inf`` / ``1_000``.
        note: Short explanation of the reject reason or clamp behaviour.
    """

    family: str
    tag: str
    id: str
    description: str
    raw: bytes
    kind: str
    seconds: float | None
    now: float | None
    clamp_max: float | None
    clamped: float | None
    float_accepts: bool
    note: str


@dataclass(frozen=True, slots=True)
class Rfc1123Vector:
    """One RFC 1123 date vector (canonical render or tolerant parse).

    Attributes:
        family: The owning family (``"rfc1123_dates"``).
        tag: Requirement-ID label (``"CFG-29"``).
        id: Unique vector id within the family.
        description: Human-readable statement of what the vector pins.
        direction: ``"render"`` (epoch -> canonical string) or ``"parse"``
            (tolerant string -> instant).
        text: The date string bytes (render output / parse input).
        epoch: The instant in whole seconds since the Unix epoch.
        round_trippable: True when rendering ``epoch`` reproduces ``text``.
    """

    family: str
    tag: str
    id: str
    description: str
    direction: str
    text: bytes
    epoch: int
    round_trippable: bool


type GoldenVector = (
    QueryCodecVector
    | RedirectVector
    | PaginationSpliceVector
    | SseVector
    | RetryPacingVector
    | Rfc1123Vector
)


# ----- resource + manifest plumbing ---------------------------------------


def _read(family: str, name: str) -> bytes:
    """Read one raw fixture file for ``family`` as bytes (never text)."""
    return files(_ANCHOR).joinpath(_DATA_SUBDIR, family, name).read_bytes()


def _entries(family: str) -> Iterator[dict[str, object]]:
    """Yield each parsed ``manifest.jsonl`` object for ``family``."""
    text = _read(family, "manifest.jsonl").decode("utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"{family}: manifest line is not an object: {line!r}")
        yield cast("dict[str, object]", obj)


def _s(entry: dict[str, object], key: str) -> str:
    value = entry[key]
    if not isinstance(value, str):
        raise TypeError(f"{key!r} must be a string, got {type(value).__name__}")
    return value


def _opt_s(entry: dict[str, object], key: str) -> str | None:
    value = entry[key]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{key!r} must be a string or null, got {type(value).__name__}")


def _bool(entry: dict[str, object], key: str) -> bool:
    value = entry[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key!r} must be a bool, got {type(value).__name__}")
    return value


def _int(entry: dict[str, object], key: str) -> int:
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key!r} must be an int, got {type(value).__name__}")
    return value


def _opt_int(entry: dict[str, object], key: str) -> int | None:
    value = entry[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key!r} must be an int or null, got {type(value).__name__}")
    return value


def _opt_float(entry: dict[str, object], key: str) -> float | None:
    value = entry[key]
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{key!r} must be a number or null, got bool")
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"{key!r} must be a number or null, got {type(value).__name__}")


def _str_list(entry: dict[str, object], key: str) -> tuple[str, ...]:
    value = entry[key]
    if not isinstance(value, list):
        raise TypeError(f"{key!r} must be a list, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{key!r} entries must be strings")
        out.append(item)
    return tuple(out)


def _parse_params(raw: bytes) -> tuple[tuple[bytes, bytes], ...]:
    """Decode a NUL-delimited ``name\\0value\\0...`` params blob."""
    if not raw:
        return ()
    fields = raw.split(b"\x00")
    if len(fields) % 2 != 0:
        raise ValueError("params blob must hold an even number of NUL-delimited fields")
    return tuple((fields[i], fields[i + 1]) for i in range(0, len(fields), 2))


# ----- per-family loaders -------------------------------------------------


def load_query_codec() -> tuple[QueryCodecVector, ...]:
    """Load every URL query codec vector."""
    out: list[QueryCodecVector] = []
    for e in _entries("query_codec"):
        out.append(
            QueryCodecVector(
                family="query_codec",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                params=_parse_params(_read("query_codec", _s(e, "params_file"))),
                query=_read("query_codec", _s(e, "query_file")),
                render_matches=_bool(e, "render_matches"),
                parse_matches=_bool(e, "parse_matches"),
            )
        )
    return tuple(out)


def load_redirect() -> tuple[RedirectVector, ...]:
    """Load every redirect ``Location`` resolution vector."""
    out: list[RedirectVector] = []
    for e in _entries("redirect"):
        out.append(
            RedirectVector(
                family="redirect",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                base=_read("redirect", _s(e, "base_file")),
                location=_read("redirect", _s(e, "location_file")),
                resolved=_s(e, "resolved").encode("ascii"),
            )
        )
    return tuple(out)


def load_pagination_splice() -> tuple[PaginationSpliceVector, ...]:
    """Load every raw-query pagination splice vector."""
    out: list[PaginationSpliceVector] = []
    for e in _entries("pagination_splice"):
        out.append(
            PaginationSpliceVector(
                family="pagination_splice",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                base=_read("pagination_splice", _s(e, "base_file")),
                param=_s(e, "param").encode("ascii"),
                value=_s(e, "value").encode("ascii"),
                spliced=_s(e, "spliced").encode("ascii"),
                untouched=tuple(s.encode("ascii") for s in _str_list(e, "untouched")),
            )
        )
    return tuple(out)


def _load_events(entry: dict[str, object]) -> tuple[SseExpectedEvent, ...]:
    raw = entry["events"]
    if not isinstance(raw, list):
        raise TypeError("'events' must be a list")
    out: list[SseExpectedEvent] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("each SSE event must be an object")
        ev = cast("dict[str, object]", item)
        out.append(
            SseExpectedEvent(
                data=_s(ev, "data"),
                event=_s(ev, "event"),
                id=_opt_s(ev, "id"),
                retry=_opt_int(ev, "retry"),
            )
        )
    return tuple(out)


def load_sse() -> tuple[SseVector, ...]:
    """Load every SSE stream vector (raw CR / LF / CRLF bytes preserved)."""
    out: list[SseVector] = []
    for e in _entries("sse"):
        out.append(
            SseVector(
                family="sse",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                stream=_read("sse", _s(e, "stream_file")),
                terminator=_s(e, "terminator"),
                events=_load_events(e),
            )
        )
    return tuple(out)


def load_retry_pacing() -> tuple[RetryPacingVector, ...]:
    """Load every ``Retry-After`` pacing vector."""
    out: list[RetryPacingVector] = []
    for e in _entries("retry_pacing"):
        out.append(
            RetryPacingVector(
                family="retry_pacing",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                raw=_read("retry_pacing", _s(e, "raw_file")),
                kind=_s(e, "kind"),
                seconds=_opt_float(e, "seconds"),
                now=_opt_float(e, "now"),
                clamp_max=_opt_float(e, "clamp_max"),
                clamped=_opt_float(e, "clamped"),
                float_accepts=_bool(e, "float_accepts"),
                note=_s(e, "note"),
            )
        )
    return tuple(out)


def load_rfc1123_dates() -> tuple[Rfc1123Vector, ...]:
    """Load every RFC 1123 date vector."""
    out: list[Rfc1123Vector] = []
    for e in _entries("rfc1123_dates"):
        out.append(
            Rfc1123Vector(
                family="rfc1123_dates",
                tag=_s(e, "tag"),
                id=_s(e, "id"),
                description=_s(e, "description"),
                direction=_s(e, "direction"),
                text=_read("rfc1123_dates", _s(e, "text_file")),
                epoch=_int(e, "epoch"),
                round_trippable=_bool(e, "round_trippable"),
            )
        )
    return tuple(out)


def load_all() -> dict[str, tuple[GoldenVector, ...]]:
    """Load every family, keyed by family name in ``FAMILIES`` order."""
    result: dict[str, tuple[GoldenVector, ...]] = {}
    result["query_codec"] = load_query_codec()
    result["redirect"] = load_redirect()
    result["pagination_splice"] = load_pagination_splice()
    result["sse"] = load_sse()
    result["retry_pacing"] = load_retry_pacing()
    result["rfc1123_dates"] = load_rfc1123_dates()
    return result


__all__ = [
    "FAMILIES",
    "FAMILY_TAGS",
    "GoldenVector",
    "PaginationSpliceVector",
    "QueryCodecVector",
    "RedirectVector",
    "RetryPacingVector",
    "Rfc1123Vector",
    "SseExpectedEvent",
    "SseVector",
    "load_all",
    "load_pagination_splice",
    "load_query_codec",
    "load_redirect",
    "load_retry_pacing",
    "load_rfc1123_dates",
    "load_sse",
]
