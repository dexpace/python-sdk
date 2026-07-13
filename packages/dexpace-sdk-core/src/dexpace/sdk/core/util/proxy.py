# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Proxy configuration value type.

``ProxyOptions`` describes an outbound HTTP / SOCKS proxy together with the
list of hosts that should bypass it. Instances are immutable and the bypass
entries are compiled exactly once at construction time so per-request
matching is a plain comparison.

Bypass entries follow the conventional ``NO_PROXY`` suffix semantics used by
curl, requests, and Go. Precedence and escaping rules:

- A single bare ``*`` short-circuits to "bypass everything" — every candidate
  host is bypassed and no per-host glob matching runs.
- An entry containing a glob metacharacter (``*`` / ``?`` / ``[``) keeps
  ``fnmatch`` semantics, so ``*.example.com`` style patterns work. To match a
  *literal* metacharacter, wrap it in a character class — ``[*]`` matches a
  literal ``*`` — which is ``fnmatch``'s escape mechanism.
- Every other (bare) entry such as ``example.com`` bypasses the host itself
  and any dot-delimited subdomain (``api.example.com``); a leading dot
  (``.example.com``) is treated identically.
- A trailing ``:port`` on either the entry or the candidate host is ignored,
  so matching is host-only and case-insensitive over the full host string.

The ``ProxyOptions.from_configuration`` factory bridges the proxy value type
to the layered ``Configuration`` lookup: it reads ``HTTPS_PROXY`` (preferred)
or ``HTTP_PROXY`` as proxy URLs and ``NO_PROXY`` as a comma-separated bypass
list. The URL scheme selects the transport flavour, scheme-less ``host:port``
forms are accepted, and percent-encoded credentials are decoded. Host and port
are always taken from the *same* resolved URL (never mixed across layers), a
missing port stays ``None`` rather than being fabricated, and credentials are
attached only over an ``https`` proxy connection. Bad proxy configuration
degrades to ``None`` rather than raising — but because a silently-unused proxy
is an outage-grade misconfiguration, an unusable value is logged at WARNING
(with any credentials redacted from the logged URL).

Environment resolution is opt-in: the plain constructor and matching methods
never read ``os.environ``. Callers that want env-based resolution ask for it
explicitly through ``from_environment`` (or ``from_configuration`` with an
env-backed ``Configuration``); the env seam is injectable for hermetic tests.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self
from urllib.parse import SplitResult, unquote, urlsplit

from ..config.configuration import Configuration, EnvSource

__all__ = ["ProxyOptions", "ProxyType"]


_LOG = logging.getLogger(__name__)

# Glob metacharacters that switch an entry into ``fnmatch`` mode.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")

# Matches the ``userinfo`` (``user:pass``) component of a URL — either after a
# ``//`` authority marker or at the start of a scheme-less ``user:pass@host``
# form — so it can be redacted before a URL is written to a log line.
_USERINFO_RE = re.compile(r"(?:^|//)([^/@]*)@")


def _redact_userinfo(url: str) -> str:
    """Mask any ``user:pass@`` credentials in a URL so logs never leak them.

    Handles ``scheme://user:pass@host``, ``//user:pass@host``, and scheme-less
    ``user:pass@host:port`` forms alike. A URL without userinfo is returned
    unchanged. This is a best-effort log-safety redaction, not a URL parser.

    Args:
        url: A proxy URL, possibly carrying credentials.

    Returns:
        The URL with any userinfo component replaced by ``***``.
    """
    match = _USERINFO_RE.search(url)
    if match is None:
        return url
    return url[: match.start(1)] + "***" + url[match.end(1) :]


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


def _resolve_endpoint(proxy_url: str) -> tuple[SplitResult, ProxyType, str, int | None] | None:
    """Resolve a proxy URL into its parsed parts, type, host, and port.

    Applies scheme->type mapping and scheme-less ``host:port`` handling. The
    port is taken verbatim from the URL: an explicit in-range port, or ``None``
    when the URL omits one — never a fabricated per-scheme default. Any value
    that cannot yield a usable endpoint is logged at WARNING (a silently-
    disabled proxy is outage-grade) with credentials redacted, and yields
    ``None``.

    Args:
        proxy_url: The raw proxy URL string.

    Returns:
        A ``(SplitResult, ProxyType, host, port)`` tuple where ``port`` is an
        ``int`` or ``None``, or ``None`` if the URL is unusable.
    """
    safe = _redact_userinfo(proxy_url)
    try:
        split = _split_proxy_url(proxy_url)
    except ValueError:
        _LOG.warning("ignoring proxy URL %s: failed to parse", safe)
        return None
    scheme = split.scheme.lower()
    proxy_type = _SCHEME_TO_TYPE["http"] if scheme == "" else _SCHEME_TO_TYPE.get(scheme)
    if proxy_type is None:
        _LOG.warning("ignoring proxy URL %s: unsupported scheme %r", safe, scheme)
        return None
    if not split.hostname or any(char.isspace() for char in split.hostname):
        _LOG.warning("ignoring proxy URL %s: missing or invalid hostname", safe)
        return None
    try:
        port = split.port
    except ValueError:
        _LOG.warning("ignoring proxy URL %s: invalid port", safe)
        return None
    return split, proxy_type, split.hostname, port


def _extract_credentials(split: SplitResult) -> tuple[str | None, str | None]:
    """Return decoded proxy credentials, but only over an ``https`` proxy.

    A proxy credential is attached only when the proxy URL scheme is
    ``https``. Sending a Proxy-Authorization credential over a plaintext
    proxy connection (``http``, scheme-less, or SOCKS) would expose it on the
    wire, so those schemes drop the credential rather than leak it. Present
    credentials are percent-decoded.

    Args:
        split: The parsed proxy URL.

    Returns:
        A ``(username, password)`` tuple; either or both may be ``None``.
    """
    if split.scheme.lower() != "https":
        return None, None
    username = unquote(split.username) if split.username is not None else None
    password = unquote(split.password) if split.password is not None else None
    return username, password


def _parse_no_proxy(raw: str | None) -> tuple[str, ...]:
    """Split a ``NO_PROXY`` value into stripped, non-empty bypass entries.

    Args:
        raw: The raw comma-separated ``NO_PROXY`` value, or ``None``.

    Returns:
        The bypass entries in order; empty when ``raw`` is absent or blank.
    """
    if raw is None or not raw.strip():
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


@dataclass(frozen=True, slots=True)
class ProxyOptions:
    """Immutable proxy configuration with pre-compiled bypass matchers.

    Attributes:
        type: Proxy transport flavour (HTTP / SOCKS4 / SOCKS5).
        host: Proxy host. Must be non-empty.
        port: Proxy port in the range ``0..65535``, or ``None`` when
            unspecified (the transport applies its own default) — the model
            never fabricates one.
        non_proxy_hosts: Bypass entries. A single bare ``*`` bypasses every
            host; a bare entry (``example.com`` or ``.example.com``) matches
            the host and its subdomains by suffix; an entry with a glob
            metacharacter (``*.example.com``) keeps ``fnmatch`` semantics. A
            trailing ``:port`` on a bare entry is ignored. Compiled once in
            ``__post_init__``.
        username: Optional username for proxy auth. Masked in every string
            form.
        password: Optional password for proxy auth. Masked in every string
            form.
    """

    type: ProxyType
    host: str
    port: int | None = None
    non_proxy_hosts: tuple[str, ...] = ()
    username: str | None = None
    password: str | None = None
    # Compiled bypass matchers and the bare-``*`` short-circuit flag. Excluded
    # from ``repr`` / equality / hashing so two ``ProxyOptions`` with the same
    # logical fields compare equal even though their compiled predicates are
    # distinct objects.
    _bypass_matchers: tuple[Callable[[str], bool], ...] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _bypass_all: bool = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        """Validate inputs and pre-compile bypass matchers.

        A single bare ``*`` entry sets the bypass-all short-circuit; the
        remaining entries are compiled into per-host matchers exactly once.

        Raises:
            ValueError: If ``host`` is empty or ``port`` is a non-``None``
                value outside 0..65535.
        """
        if not self.host:
            raise ValueError("host must not be empty")
        if self.port is not None and not (0 <= self.port <= 65535):
            raise ValueError(f"port must be in 0..65535, got {self.port}")
        bypass_all = any(entry.strip() == "*" for entry in self.non_proxy_hosts)
        object.__setattr__(self, "_bypass_all", bypass_all)
        compiled = tuple(
            _compile_bypass(pattern) for pattern in self.non_proxy_hosts if pattern.strip() != "*"
        )
        object.__setattr__(self, "_bypass_matchers", compiled)

    def bypasses_proxy(self, host: str) -> bool:
        """Return ``True`` when ``host`` matches any bypass entry.

        A single bare ``*`` in ``non_proxy_hosts`` short-circuits to ``True``
        for every host without running per-host matching. Otherwise matching
        is case-insensitive over the full host string — hostnames on the wire
        are case-insensitive per RFC 3986. Bare entries use suffix semantics
        (``example.com`` bypasses ``api.example.com``); glob entries use
        ``fnmatch``. A trailing ``:port`` on the candidate is stripped before
        matching, so port-qualified hosts compare on their host part alone.

        Args:
            host: Candidate hostname, with an optional ``:port`` suffix
                (stripped) and optional IPv6 brackets. No scheme.

        Returns:
            ``True`` if the bypass-all short-circuit is set or at least one
            bypass entry matches; ``False`` otherwise (including when there
            are no bypass entries).
        """
        if self._bypass_all:
            return True
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

    def __str__(self) -> str:
        """Render the proxy options with credentials masked.

        Delegates to ``__repr__`` so ``str(options)`` and ``f"{options}"``
        carry the same credential masking — no string form ever leaks the
        proxy password.

        Returns:
            The credential-masked ``ProxyOptions(...)`` rendering.
        """
        return self.__repr__()

    @classmethod
    def from_environment(cls, env: EnvSource = os.environ.get) -> Self | None:
        """Resolve proxy settings from environment variables (opt-in).

        Environment resolution is opt-in: the plain constructor and matching
        methods never read ``os.environ`` implicitly. Callers ask for env-based
        resolution explicitly here. The ``env`` seam is injectable so tests can
        exercise resolution hermetically without mutating the real process
        environment.

        Args:
            env: Callable mapping a variable name to its value or ``None``.
                Defaults to ``os.environ.get``; pass a fake mapping's ``get``
                in tests.

        Returns:
            A populated ``ProxyOptions``, or ``None`` when no proxy is
            configured or the value is unusable.
        """
        return cls.from_configuration(Configuration(env=env))

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
        - A missing port stays ``None`` instead of being fabricated by scheme;
          host and port always come from the same resolved URL.
        - A scheme-less ``proxy:8080`` is parsed as host:port (assumed HTTP).
        - Credentials are attached only over an ``https`` proxy and are
          ``unquote()``-decoded; over a plaintext (``http`` / scheme-less /
          SOCKS) proxy they are dropped rather than sent in the clear.

        Because a silently-unused proxy is an outage-grade misconfiguration, a
        genuinely unusable proxy value is logged at WARNING (with any
        credentials redacted). Resolution never raises: a malformed value
        degrades to ``None``.

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
        username, password = _extract_credentials(split)
        try:
            return cls(
                type=proxy_type,
                host=host,
                port=port,
                non_proxy_hosts=_parse_no_proxy(no_proxy_raw),
                username=username,
                password=password,
            )
        except ValueError:
            safe = _redact_userinfo(proxy_url)
            _LOG.warning("ignoring proxy URL %s: failed ProxyOptions validation", safe)
            return None
