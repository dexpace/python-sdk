# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure RFC 3986 query-string codec (furl-free).

Wire-exact query rendering and lenient parsing for URL query strings, kept in a
standalone module that imports no `furl` so the byte-level rendering path never
picks up furl's own normalisations (default-port stripping, `+`/`%20`
reinterpretation, unicode host forms). The `Url` / `QueryParams` public API in
`url` calls into this module for the wire-exact query paths; furl there handles
only the scheme/authority/path parse-and-serialise plumbing.

Rendering is strict. Only the RFC 3986 `unreserved` set (`A-Za-z0-9-._~`) is
emitted verbatim; every other byte — space, `+`, the sub-delims and gen-delims,
and all non-ASCII (UTF-8 first) — is percent-encoded with upper-case hex. A
space becomes `%20` (never `+`) and a literal `+` becomes `%2B`, so the output
is a true RFC 3986 query component rather than an
`application/x-www-form-urlencoded` payload (whose plus-for-space rule is the
trap this module exists to avoid).

Parsing is lenient. Pairs split on `&`, then name and value split on the first
`=`. A wire `+` stays a literal `+` — it is NOT form-decoded to a space — and a
token with no `=` yields a present empty-string value, distinct from an absent
name. `parse_query(render_query(pairs)) == pairs` for every pair sequence.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote, unquote

__all__ = ["parse_query", "render_query"]


def _encode(component: str) -> str:
    """Percent-encode one query component, leaving only ``unreserved`` verbatim.

    ``urllib.parse.quote``'s always-safe set is exactly the RFC 3986
    ``unreserved`` set (``A-Za-z0-9-._~``), so ``safe=""`` percent-encodes every
    other byte with upper-case hex: a space becomes ``%20``, ``+`` becomes
    ``%2B``, the reserved delimiters become ``%XX``, and non-ASCII is UTF-8
    encoded first. ``quote`` never emits ``+`` for a space, unlike ``quote_plus``
    / ``urlencode``.

    Args:
        component: A raw (decoded) query name or value.

    Returns:
        The percent-encoded component.
    """
    return quote(component, safe="")


def render_query(pairs: Iterable[tuple[str, str]]) -> str:
    """Render ordered ``(name, value)`` pairs to a strict RFC 3986 query string.

    Each pair is emitted as ``name=value`` — always including the ``=`` — in the
    given order, joined by ``&``; pairs are never sorted. Names and values are
    percent-encoded so only the ``unreserved`` set survives, a space renders as
    ``%20`` and a ``+`` as ``%2B``.

    Args:
        pairs: Ordered ``(name, value)`` pairs. A present-but-empty value
            renders as ``name=`` (distinct from the name being absent, which is
            simply not supplying the pair).

    Returns:
        The wire query string with no leading ``?`` (empty for no pairs).
    """
    return "&".join(f"{_encode(name)}={_encode(value)}" for name, value in pairs)


def parse_query(raw: str) -> list[tuple[str, str]]:
    """Parse a query string into ordered ``(name, value)`` pairs (lenient).

    Splits on ``&`` then on the first ``=`` per token, preserving order. Percent
    escapes are decoded as UTF-8; a wire ``+`` is kept as a literal ``+`` rather
    than form-decoded to a space. A token with no ``=`` (a bare flag) yields a
    present empty-string value, distinct from an absent name.

    Args:
        raw: A query string with no leading ``?`` (an empty string yields no
            pairs).

    Returns:
        The ordered ``(name, value)`` pairs, one per ``&``-separated token.
    """
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for token in raw.split("&"):
        name, sep, value = token.partition("=")
        pairs.append((unquote(name), unquote(value) if sep else ""))
    return pairs
