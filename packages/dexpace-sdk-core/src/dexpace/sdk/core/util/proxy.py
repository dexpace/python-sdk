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
(preferred) or ``HTTP_PROXY`` as full URLs and ``NO_PROXY`` as a
comma-separated bypass list. Parse failures degrade to ``None`` rather than
raising — bad proxy configuration should never bring down the caller.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from ..config.configuration import Configuration

__all__ = ["ProxyOptions", "ProxyType"]


_LOG = logging.getLogger(__name__)

# Glob metacharacters that switch an entry into ``fnmatch`` mode.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")


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
    ``.example.com`` and ``example.com`` behave identically, and a trailing
    ``:port`` is dropped so ``example.com:443`` matches on its host part.

    Args:
        pattern: A raw ``NO_PROXY`` list entry (already stripped).

    Returns:
        A predicate mapping a lower-cased candidate host to a bypass boolean.
    """
    if any(char in pattern for char in _GLOB_CHARS):
        regex = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
        return lambda host: regex.match(host) is not None
    suffix = _strip_port(pattern).lstrip(".").lower()
    dotted = "." + suffix

    def matches(host: str) -> bool:
        candidate = host.lower()
        return candidate == suffix or candidate.endswith(dotted)

    return matches


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

        Reads ``HTTPS_PROXY`` (preferred) or ``HTTP_PROXY`` as full proxy
        URLs (``http://user:pass@proxy.corp:8080``). Reads ``NO_PROXY`` as
        a comma-separated bypass list. A ``NO_PROXY`` value of ``"*"``
        bypasses everything and short-circuits to ``None``.

        Args:
            config: Layered configuration to read from.

        Returns:
            A populated ``ProxyOptions``, or ``None`` when no proxy is
            configured, when ``NO_PROXY=*``, or when the proxy URL fails to
            parse (a debug-level log line records the failure).
        """
        no_proxy_raw = config.get(Configuration.NO_PROXY)
        if no_proxy_raw is not None and no_proxy_raw.strip() == "*":
            return None
        proxy_url = config.get(Configuration.HTTPS_PROXY) or config.get(Configuration.HTTP_PROXY)
        if proxy_url is None or proxy_url == "":
            return None
        try:
            parsed = urlsplit(proxy_url)
        except ValueError:
            _LOG.debug("failed to parse proxy URL %r", proxy_url)
            return None
        if not parsed.hostname:
            _LOG.debug("proxy URL %r missing hostname", proxy_url)
            return None
        try:
            port = parsed.port
        except ValueError:
            _LOG.debug("proxy URL %r has invalid port", proxy_url)
            return None
        if port is None:
            _LOG.debug("proxy URL %r missing port", proxy_url)
            return None
        non_proxy_hosts: tuple[str, ...] = ()
        if no_proxy_raw is not None and no_proxy_raw.strip():
            non_proxy_hosts = tuple(
                entry.strip() for entry in no_proxy_raw.split(",") if entry.strip()
            )
        try:
            return cls(
                type=ProxyType.HTTP,
                host=parsed.hostname,
                port=port,
                non_proxy_hosts=non_proxy_hosts,
                username=parsed.username,
                password=parsed.password,
            )
        except ValueError:
            _LOG.debug("proxy URL %r failed ProxyOptions validation", proxy_url)
            return None
