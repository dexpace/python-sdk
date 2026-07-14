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

__all__ = ["parse_query", "remove_query_param", "render_query", "splice_query"]


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


def _token_name(token: str) -> str:
    """Decode a raw ``name=value`` token's name (the part before the first ``=``).

    The name is decoded so it compares against a plain (unencoded) parameter
    name, while the token itself is left untouched by the caller — only the
    name is inspected, never rewritten.

    Args:
        token: One raw ``&``-separated query token (a bare flag has no ``=``).

    Returns:
        The percent-decoded parameter name.
    """
    return unquote(token.split("=", 1)[0])


def splice_query(raw_query: str, name: str, value: str) -> str:
    """Set ``name=value`` in a raw query, preserving every other token verbatim.

    A wire-exact splice: the query is tokenised on ``&`` and every untouched
    token is copied through byte for byte — a pre-encoded ``%20`` survives and a
    literal ``+`` is neither re-encoded to ``%2B`` nor decoded to a space. Only
    the new pair is freshly RFC 3986 encoded via ``render_query``. This is a
    deliberate alternative to a ``parse_qs`` / ``urlencode`` round-trip, which
    would normalise (and so corrupt) the untouched bytes.

    The first token whose (decoded) name equals ``name`` is replaced in place,
    keeping its position; if no token matches, the new pair is appended. A
    second occurrence of ``name`` is left untouched — a "set" edits the first
    only.

    Args:
        raw_query: The raw query string with no leading ``?`` (may be empty).
        name: The parameter name to set (plain, unencoded).
        value: The parameter value to set (plain, unencoded).

    Returns:
        The rewritten raw query string with no leading ``?``.
    """
    encoded = render_query([(name, value)])
    if not raw_query:
        return encoded
    result: list[str] = []
    replaced = False
    for token in raw_query.split("&"):
        if not replaced and _token_name(token) == name:
            result.append(encoded)
            replaced = True
        else:
            result.append(token)
    if not replaced:
        result.append(encoded)
    return "&".join(result)


def remove_query_param(raw_query: str, name: str) -> str:
    """Drop every occurrence of ``name`` from a raw query, preserving other tokens.

    The counterpart to ``splice_query``'s "set": all tokens whose (decoded) name
    equals ``name`` are removed; every other token is copied through byte for
    byte, so untouched pre-encoded or literal-``+`` bytes survive verbatim.

    Args:
        raw_query: The raw query string with no leading ``?`` (may be empty).
        name: The parameter name to drop (plain, unencoded).

    Returns:
        The rewritten raw query string with no leading ``?``.
    """
    if not raw_query:
        return ""
    kept = [token for token in raw_query.split("&") if _token_name(token) != name]
    return "&".join(kept)


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
