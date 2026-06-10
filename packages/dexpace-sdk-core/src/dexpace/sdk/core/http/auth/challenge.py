# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``WWW-Authenticate`` / ``Proxy-Authenticate`` challenge model + parser.

The parser implements a tolerant subset of RFC 7235 §2.1 sufficient for real
server responses: it splits a header into one or more challenges and recovers
the scheme + parameter map for each. Quoted-string values are unquoted and
``quoted-pair`` escapes (``\\X`` → ``X``) decoded per RFC 7230 §3.2.6.

Auth-params within a challenge must be comma-separated; this parser does not
recover whitespace-separated parameters. ``token68`` credentials (as used by
the ``Negotiate`` and ``NTLM`` schemes) are not supported — only ``scheme
auth-param`` challenges are recognised.

Malformed tokens are skipped rather than aborting the parse; callers see
whatever valid challenges were extracted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class AuthenticateChallenge:
    """A single parsed authentication challenge.

    Attributes:
        scheme: The auth scheme name as it appeared on the wire (e.g.
            ``"Basic"``, ``"Digest"``, ``"Bearer"``). Compare with
            ``casefold()`` for case-insensitive matching.
        parameters: Read-only parameter map for the challenge. Keys are
            lower-cased on parse so lookups are case-insensitive per RFC 7235
            §2.1. Values preserve their original casing.
    """

    scheme: str
    parameters: Mapping[str, str]


def parse_challenges(header_value: str) -> list[AuthenticateChallenge]:
    """Parse a ``WWW-Authenticate`` or ``Proxy-Authenticate`` header.

    Auth-params within a challenge are recognised only when comma-separated;
    whitespace-separated params are not recovered. ``token68`` credentials
    (e.g. ``Negotiate``/``NTLM``) are unsupported and yield no parameters.

    Args:
        header_value: The full header value (a single line concatenated
            across folded continuations is fine — RFC 7230 deprecates
            folding but tolerant parsers accept it).

    Returns:
        Zero or more parsed challenges. Malformed segments are skipped
        silently; any valid challenge found is returned.
    """
    tokens = _split_top_level(header_value)
    challenges: list[AuthenticateChallenge] = []
    current_scheme: str | None = None
    current_params: dict[str, str] = {}
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        scheme, params, _has_params = _try_parse_scheme_start(token)
        if scheme is not None:
            if current_scheme is not None:
                challenges.append(_freeze(current_scheme, current_params))
            current_scheme = scheme
            current_params = params
            continue
        # Otherwise this token is a continuation parameter for the current
        # challenge: ``key=value`` or ``key="quoted"``.
        if current_scheme is None:
            continue
        key, value = _try_parse_param(token)
        if key is None or value is None:
            continue
        current_params[key] = value
    if current_scheme is not None:
        challenges.append(_freeze(current_scheme, current_params))
    return challenges


def _freeze(scheme: str, params: dict[str, str]) -> AuthenticateChallenge:
    """Build a challenge whose parameters are a read-only view."""
    return AuthenticateChallenge(scheme=scheme, parameters=MappingProxyType(dict(params)))


def _split_top_level(value: str) -> list[str]:
    """Split on commas at the top level, respecting quoted strings."""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_quote:
            if ch == "\\":
                buf.append(ch)
                escaped = True
                continue
            buf.append(ch)
            if ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
            continue
        if ch == ",":
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _try_parse_scheme_start(
    token: str,
) -> tuple[str | None, dict[str, str], bool]:
    """Detect a ``scheme [param]`` token.

    A scheme is recognised when the first whitespace-delimited word is a
    valid token AND it is not itself a ``key=value`` pair. ``token68``
    credentials (e.g. ``Negotiate``/``NTLM``) are not parsed — only
    comma-separated ``key=value`` auth-params are recovered. Returns
    ``(scheme, params, has_params)``. If ``token`` is purely a continuation
    parameter, returns ``(None, {}, False)``.
    """
    # If the token contains no whitespace and an ``=``, treat it as a
    # parameter, not a scheme.
    stripped = token.lstrip()
    if not stripped:
        return None, {}, False
    # Find first whitespace; if absent and an ``=`` exists, it's a param.
    first_space = -1
    for i, ch in enumerate(stripped):
        if ch.isspace():
            first_space = i
            break
        if ch == "=":
            return None, {}, False
    if first_space == -1:
        # Bare scheme with no parameters (e.g. ``Negotiate``).
        if not _is_token(stripped):
            return None, {}, False
        return stripped, {}, False
    head = stripped[:first_space]
    rest = stripped[first_space + 1 :].lstrip()
    if not _is_token(head):
        return None, {}, False
    # ``rest`` should look like one or more ``key=value`` pairs separated
    # by whitespace. If it instead looks like ``=value`` we're actually a
    # parameter where the key happens to share characters with a token.
    if rest.startswith("="):
        return None, {}, False
    params: dict[str, str] = {}
    for raw in _split_param_list(rest):
        key, value = _try_parse_param(raw)
        if key is not None and value is not None:
            params[key] = value
    return head, params, True


def _split_param_list(value: str) -> list[str]:
    """Split a comma-separated parameter list at the top level.

    Auth-params must be comma-separated; whitespace alone does not delimit
    parameters, so a whitespace-separated run collapses into one segment.
    """
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_quote:
            buf.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
            continue
        if ch in (",",):
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _try_parse_param(token: str) -> tuple[str | None, str | None]:
    """Parse ``key=value`` (value may be quoted) into a (key, value) tuple."""
    token = token.strip()
    if not token:
        return None, None
    eq = token.find("=")
    if eq <= 0:
        return None, None
    key = token[:eq].strip()
    value = token[eq + 1 :].strip()
    if not _is_token(key):
        return None, None
    if value.startswith('"'):
        return key.lower(), _unquote(value)
    # token68 / unquoted token value
    if not value:
        return None, None
    return key.lower(), value


def _unquote(value: str) -> str:
    """Decode a quoted-string per RFC 7230 §3.2.6.

    Returns the bare value when the input is malformed (missing close quote).
    """
    if not value.startswith('"'):
        return value
    # Find matching close quote, skipping escaped quotes.
    out: list[str] = []
    i = 1
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            out.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out)
        out.append(ch)
        i += 1
    # Malformed (no closing quote): return what we have so the rest of the
    # parse can still proceed.
    return "".join(out)


# RFC 7230 token: 1*tchar
_TCHARS = frozenset(
    "!#$%&'*+-.^_`|~" + "0123456789" + "abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def _is_token(value: str) -> bool:
    return bool(value) and all(c in _TCHARS for c in value)


__all__ = [
    "AuthenticateChallenge",
    "parse_challenges",
]
