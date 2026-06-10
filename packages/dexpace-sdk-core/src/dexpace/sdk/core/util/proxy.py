# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Proxy configuration value type.

``ProxyOptions`` describes an outbound HTTP / SOCKS proxy together with the
list of hosts that should bypass it. Instances are immutable and the bypass
entries are compiled exactly once at construction time so per-request
matching is a plain comparison.

Bypass entries follow the conventional ``NO_PROXY`` suffix semantics used by
curl, requests, and Go: a bare entry such as ``example.com`` bypasses the
host itself and any dot-delimited subdomain (``api.example.com``); a leading
dot (``.example.com``) is treated identically. A trailing ``:port`` on either
the entry or the candidate host is ignored, so matching is host-only. Entries
containing a glob metacharacter (``*`` / ``?`` / ``[``) keep their ``fnmatch``
behaviour so existing ``*.example.com`` style patterns continue to work.

The ``ProxyOptions.from_configuration`` factory bridges the proxy value
type to the layered ``Configuration`` lookup: it reads ``HTTPS_PROXY``
(preferred) or ``HTTP_PROXY`` as proxy URLs and ``NO_PROXY`` as a
comma-separated bypass list. The URL scheme selects the transport flavour, a
missing port defaults by scheme, scheme-less ``host:port`` forms are accepted,
and percent-encoded credentials are decoded. Bad proxy configuration degrades
to ``None`` rather than raising — but because a silently-unused proxy is an
outage-grade misconfiguration, an unusable value is logged at WARNING.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self
from urllib.parse import SplitResult, unquote, urlsplit

from ..config.configuration import Configuration

__all__ = ["ProxyOptions", "ProxyType"]


_LOG = logging.getLogger(__name__)

# Glob metacharacters that switch an entry into ``fnmatch`` mode.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")

# Default proxy port per URL scheme when the proxy URL omits one. SOCKS
# proxies conventionally listen on 1080.
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "socks4": 1080,
    "socks5": 1080,
    "socks5h": 1080,
}


def _strip_port(host: str) -> str:
    """Drop a trailing ``:port`` (and IPv6 brackets) so matching is host-only.

    A bracketed IPv6 literal (``[::1]`` or ``[::1]:443``) yields its inner
    address; a ``host:port`` carrying a single colon drops the port; a bare
    IPv6 literal (multiple colons, no port) is returned unchanged.

    Args:
        host: A candidate host or a bypass entry, possibly port-qualified.

    Returns:
        The host with any port and IPv6 brackets removed.
    """
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name
    return host


def _compile_bypass(pattern: str) -> Callable[[str], bool]:
    """Compile a single ``NO_PROXY`` entry into a case-insensitive matcher.

    Entries containing a glob metacharacter (``*`` / ``?`` / ``[``) keep
    their ``fnmatch`` semantics. Every other (bare) entry uses conventional
    suffix matching: a candidate matches when it equals the entry or ends
    with ``"." + entry``. Leading dot(s) on the entry are stripped so
    ``.example.com`` and ``example.com`` behave identically. A trailing
    ``:port`` is dropped from both bare and glob entries — candidate hosts are
    matched on their host part alone, so a ported glob like
    ``*.example.com:443`` would otherwise never match.

    Args:
        pattern: A raw ``NO_PROXY`` list entry (already stripped).

    Returns:
        A predicate mapping a lower-cased candidate host to a bypass boolean.
    """
    if any(char in pattern for char in _GLOB_CHARS):
        glob = _strip_port(pattern)
        regex = re.compile(fnmatch.translate(glob), re.IGNORECASE)
        return lambda host: regex.match(host) is not None
    suffix = _strip_port(pattern).lstrip(".").lower()
    dotted = "." + suffix

    def matches(host: str) -> bool:
        candidate = host.lower()
        return candidate == suffix or candidate.endswith(dotted)

    return matches


def _split_proxy_url(proxy_url: str) -> SplitResult:
    """Parse a proxy URL, tolerating a missing ``scheme://`` prefix.

    ``urlsplit`` parses a scheme-less ``proxy:8080`` as scheme ``proxy`` with
    path ``8080`` — losing the host and port entirely. When the value has no
    recognised ``scheme://`` authority marker, this prepends ``//`` so the
    whole value is parsed as a network location (host:port), matching how
    callers conventionally write a bare proxy address.

    Args:
        proxy_url: A proxy URL, possibly scheme-less.

    Returns:
        The ``SplitResult`` of parsing the (possibly normalised) URL.
    """
    if "//" not in proxy_url:
        return urlsplit("//" + proxy_url)
    return urlsplit(proxy_url)


class ProxyType(StrEnum):
    """Supported proxy transport flavours.

    The SDK core only models the *type* — concrete transports decide which
    flavours they actually support. ``SOCKS4`` / ``SOCKS5`` are included for
    API parity with the Java SDK even though the stdlib HTTP adapter only
    speaks ``HTTP``.
    """

    HTTP = "HTTP"
    SOCKS4 = "SOCKS4"
    SOCKS5 = "SOCKS5"


# Map a proxy URL scheme to the modelled transport flavour. A scheme absent
# from this table is unsupported and is rejected (with a WARNING) rather than
# silently downgraded to HTTP. ``socks5h`` (remote DNS) maps to ``SOCKS5``.
_SCHEME_TO_TYPE: dict[str, ProxyType] = {
    "http": ProxyType.HTTP,
    "https": ProxyType.HTTP,
    "socks4": ProxyType.SOCKS4,
    "socks5": ProxyType.SOCKS5,
    "socks5h": ProxyType.SOCKS5,
}


def _resolve_endpoint(proxy_url: str) -> tuple[SplitResult, ProxyType, str, int] | None:
    """Resolve a proxy URL into its parsed parts, type, host, and port.

    Applies scheme→type mapping, scheme-by-default port resolution, and
    scheme-less ``host:port`` handling. Any value that cannot yield a usable
    endpoint is logged at WARNING (a silently-disabled proxy is outage-grade)
    and yields ``None``.

    Args:
        proxy_url: The raw proxy URL string.

    Returns:
        A ``(SplitResult, ProxyType, host, port)`` tuple, or ``None`` if the
        URL is unusable.
    """
    try:
        split = _split_proxy_url(proxy_url)
    except ValueError:
        _LOG.warning("ignoring proxy URL %r: failed to parse", proxy_url)
        return None
    scheme = split.scheme.lower()
    proxy_type = _SCHEME_TO_TYPE["http"] if scheme == "" else _SCHEME_TO_TYPE.get(scheme)
    if proxy_type is None:
        _LOG.warning("ignoring proxy URL %r: unsupported scheme %r", proxy_url, scheme)
        return None
    if not split.hostname:
        _LOG.warning("ignoring proxy URL %r: missing hostname", proxy_url)
        return None
    try:
        port = split.port
    except ValueError:
        _LOG.warning("ignoring proxy URL %r: invalid port", proxy_url)
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 80)
    return split, proxy_type, split.hostname, port


@dataclass(frozen=True, slots=True)
class ProxyOptions:
    """Immutable proxy configuration with pre-compiled bypass matchers.

    Attributes:
        type: Proxy transport flavour (HTTP / SOCKS4 / SOCKS5).
        host: Proxy host. Must be non-empty.
        port: Proxy port in the range ``0..65535``.
        non_proxy_hosts: Bypass entries. A bare entry (``example.com`` or
            ``.example.com``) matches the host and its subdomains by suffix;
            an entry with a glob metacharacter (``*.example.com``) keeps
            ``fnmatch`` semantics. A trailing ``:port`` on a bare entry is
            ignored. Compiled once in ``__post_init__``.
        username: Optional username for proxy auth. Masked in ``repr``.
        password: Optional password for proxy auth. Masked in ``repr``.
    """

    type: ProxyType
    host: str
    port: int
    non_proxy_hosts: tuple[str, ...] = ()
    username: str | None = None
    password: str | None = None
    # Compiled bypass matchers. Excluded from ``repr`` / equality / hashing so
    # two ``ProxyOptions`` with the same logical fields compare equal even
    # though their compiled predicates are distinct objects.
    _bypass_matchers: tuple[Callable[[str], bool], ...] = field(
        init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        """Validate inputs and pre-compile bypass matchers.

        Raises:
            ValueError: If ``host`` is empty or ``port`` is outside 0..65535.
        """
        if not self.host:
            raise ValueError("host must not be empty")
        if not (0 <= self.port <= 65535):
            raise ValueError(f"port must be in 0..65535, got {self.port}")
        compiled = tuple(_compile_bypass(pattern) for pattern in self.non_proxy_hosts)
        object.__setattr__(self, "_bypass_matchers", compiled)

    def bypasses_proxy(self, host: str) -> bool:
        """Return ``True`` when ``host`` matches any bypass entry.

        Matching is case-insensitive — hostnames on the wire are
        case-insensitive per RFC 3986. Bare entries use suffix semantics
        (``example.com`` bypasses ``api.example.com``); glob entries use
        ``fnmatch``. A trailing ``:port`` on the candidate is stripped before
        matching, so port-qualified hosts compare on their host part alone.

        Args:
            host: Candidate hostname, with an optional ``:port`` suffix
                (stripped) and optional IPv6 brackets. No scheme.

        Returns:
            ``True`` if at least one bypass entry matches; ``False``
            otherwise (including when there are no bypass entries).
        """
        candidate = _strip_port(host)
        return any(matcher(candidate) for matcher in self._bypass_matchers)

    def __repr__(self) -> str:
        """Render the proxy options with credentials masked.

        Username and password (when present) are rendered as ``'***'`` so
        accidental logging of the proxy configuration never leaks creds.

        Returns:
            A ``ProxyOptions(...)`` repr suitable for logs.
        """
        username = "'***'" if self.username is not None else "None"
        password = "'***'" if self.password is not None else "None"
        return (
            f"ProxyOptions(type={self.type!r}, host={self.host!r}, "
            f"port={self.port!r}, non_proxy_hosts={self.non_proxy_hosts!r}, "
            f"username={username}, password={password})"
        )

    @classmethod
    def from_configuration(cls, config: Configuration) -> Self | None:
        """Build a ``ProxyOptions`` from layered configuration env vars.

        Reads ``HTTPS_PROXY`` (preferred) or ``HTTP_PROXY`` as proxy URLs and
        ``NO_PROXY`` as a comma-separated bypass list. A ``NO_PROXY`` value of
        ``"*"`` bypasses everything and short-circuits to ``None``.

        The proxy URL is parsed leniently so common real-world forms work:

        - The URL ``scheme`` selects the transport flavour: ``http``/``https``
          map to ``HTTP``, ``socks4`` to ``SOCKS4``, ``socks5``/``socks5h`` to
          ``SOCKS5``. An *unsupported* scheme is rejected (logged at WARNING),
          never silently downgraded to HTTP.
        - A missing port defaults by scheme (``http`` 80, ``https`` 443, SOCKS
          1080) instead of dropping the proxy.
        - A scheme-less ``proxy:8080`` is parsed as host:port (assumed HTTP).
        - Percent-encoded credentials are ``unquote()``-decoded.

        Because a silently-unused proxy is an outage-grade misconfiguration, a
        genuinely unusable proxy value is logged at WARNING (not DEBUG).

        Args:
            config: Layered configuration to read from.

        Returns:
            A populated ``ProxyOptions``, or ``None`` when no proxy is
            configured, when ``NO_PROXY=*``, or when the proxy URL is
            unusable (a WARNING log line records why).
        """
        no_proxy_raw = config.get(Configuration.NO_PROXY)
        if no_proxy_raw is not None and no_proxy_raw.strip() == "*":
            return None
        proxy_url = config.get(Configuration.HTTPS_PROXY) or config.get(Configuration.HTTP_PROXY)
        if not proxy_url:
            return None
        endpoint = _resolve_endpoint(proxy_url)
        if endpoint is None:
            return None
        split, proxy_type, host, port = endpoint
        non_proxy_hosts: tuple[str, ...] = ()
        if no_proxy_raw is not None and no_proxy_raw.strip():
            non_proxy_hosts = tuple(
                entry.strip() for entry in no_proxy_raw.split(",") if entry.strip()
            )
        username = unquote(split.username) if split.username is not None else None
        password = unquote(split.password) if split.password is not None else None
        try:
            return cls(
                type=proxy_type,
                host=host,
                port=port,
                non_proxy_hosts=non_proxy_hosts,
                username=username,
                password=password,
            )
        except ValueError:
            _LOG.warning("ignoring proxy URL %r: failed ProxyOptions validation", proxy_url)
            return None
