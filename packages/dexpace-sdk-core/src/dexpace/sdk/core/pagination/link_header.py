# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""RFC 5988 ``Link`` header parser — pure string logic, no I/O.

A ``Link`` header carries one or more comma-separated link-values, each a
URI-Reference in angle brackets followed by semicolon-separated parameters::

    Link: <https://api.example.com/items?page=2>; rel="next",
          <https://api.example.com/items?page=9>; rel="last"

This module parses that grammar into ``(target, params)`` pairs and exposes a
convenience lookup keyed by ``rel`` value. It is deliberately standalone: the
standard library has no equivalent, and the paginator's link-header strategy
depends only on this pure function.
"""

from __future__ import annotations

from collections.abc import Iterator

#: A parsed link-value: the bracketed target URI plus its lower-cased
#: parameter map (parameter names are case-insensitive per RFC 5988 §5).
type ParsedLink = tuple[str, dict[str, str]]


def parse_link_header(value: str) -> tuple[ParsedLink, ...]:
    """Parse an RFC 5988 ``Link`` header into its link-values.

    Args:
        value: The raw header value (without the ``Link:`` name). An empty or
            whitespace-only string yields an empty result.

    Returns:
        One ``(target, params)`` pair per link-value, in source order.
        ``params`` keys are lower-cased; quoted values are unquoted.
    """
    return tuple(_iter_links(value))


def find_rel(value: str, rel: str) -> str | None:
    """Return the target URI of the first link-value whose ``rel`` matches.

    The ``rel`` parameter is a space-separated set of relation types
    (RFC 5988 §5.3); a match succeeds when ``rel`` appears as one of those
    types. Comparison is case-insensitive.

    Args:
        value: The raw ``Link`` header value.
        rel: The relation type to look for (e.g. ``"next"``).

    Returns:
        The matching target URI, or ``None`` when no link-value carries the
        requested relation.
    """
    wanted = rel.casefold()
    for target, params in _iter_links(value):
        rels = params.get("rel", "")
        if any(token.casefold() == wanted for token in rels.split()):
            return target
    return None


def _iter_links(value: str) -> Iterator[ParsedLink]:
    for segment in _split_links(value):
        parsed = _parse_link_value(segment)
        if parsed is not None:
            yield parsed


def _split_links(value: str) -> Iterator[str]:
    """Split a header into link-value segments on the link-value separators.

    Commas inside a quoted parameter value (between ``"``) or inside the
    bracketed ``<URI>`` target are not separators: a comma is a legal
    unencoded URI sub-delim (e.g. ``?fields=a,b``), so splitting on it would
    shred the target. The angle-bracket depth is tracked alongside quote
    state and suppresses the split while inside ``<...>``.
    """
    buffer: list[str] = []
    in_quotes = False
    in_brackets = False
    escaped = False
    for char in value:
        if escaped:
            buffer.append(char)
            escaped = False
        elif char == "\\":
            buffer.append(char)
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
        elif char == "<" and not in_quotes:
            in_brackets = True
            buffer.append(char)
        elif char == ">" and not in_quotes:
            in_brackets = False
            buffer.append(char)
        elif char == "," and not in_quotes and not in_brackets:
            yield "".join(buffer)
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        yield "".join(buffer)


def _parse_link_value(segment: str) -> ParsedLink | None:
    segment = segment.strip()
    if not segment.startswith("<"):
        return None
    end = segment.find(">")
    if end < 0:
        return None
    target = segment[1:end].strip()
    params = _parse_params(segment[end + 1 :])
    return target, params


def _parse_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in _split_params(raw):
        name, sep, val = part.partition("=")
        name = name.strip().casefold()
        if not name or not sep:
            continue
        params[name] = _unquote(val.strip())
    return params


def _split_params(raw: str) -> Iterator[str]:
    buffer: list[str] = []
    in_quotes = False
    escaped = False
    for char in raw:
        if escaped:
            buffer.append(char)
            escaped = False
        elif char == "\\":
            buffer.append(char)
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
        elif char == ";" and not in_quotes:
            if buffer:
                yield "".join(buffer)
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        yield "".join(buffer)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


__all__ = ["ParsedLink", "find_rel", "parse_link_header"]
