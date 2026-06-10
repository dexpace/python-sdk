# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the `dexpace.sdk.core.util.proxy` module."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.config.configuration import Configuration
from dexpace.sdk.core.util import ProxyOptions, ProxyType


def test_proxy_options_basic() -> None:
    """A bare ``ProxyOptions`` exposes its constructor arguments verbatim."""
    options = ProxyOptions(type=ProxyType.HTTP, host="proxy.corp", port=8080)
    assert options.type is ProxyType.HTTP
    assert options.host == "proxy.corp"
    assert options.port == 8080
    assert options.non_proxy_hosts == ()
    assert options.username is None
    assert options.password is None


def test_proxy_options_port_validation() -> None:
    """Negative or out-of-range port values raise ``ValueError``."""
    with pytest.raises(ValueError):
        ProxyOptions(type=ProxyType.HTTP, host="proxy", port=-1)
    with pytest.raises(ValueError):
        ProxyOptions(type=ProxyType.HTTP, host="proxy", port=65536)


def test_proxy_options_empty_host_validation() -> None:
    """An empty host string is rejected at construction."""
    with pytest.raises(ValueError):
        ProxyOptions(type=ProxyType.HTTP, host="", port=8080)


def test_bypasses_proxy_exact_match() -> None:
    """A ``*.suffix`` glob matches any single-label subdomain."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("*.internal.example.com",),
    )
    assert options.bypasses_proxy("api.internal.example.com") is True


def test_bypasses_proxy_case_insensitive() -> None:
    """Glob matching ignores case on both pattern and candidate host."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("*.example.com",),
    )
    assert options.bypasses_proxy("API.EXAMPLE.COM") is True


def test_bypasses_proxy_no_match() -> None:
    """Unrelated hosts are not bypassed."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("*.internal.example.com",),
    )
    assert options.bypasses_proxy("api.example.org") is False


def test_bypasses_proxy_bare_entry_matches_subdomains() -> None:
    """A bare ``example.com`` entry follows curl/Go suffix semantics."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("example.com",),
    )
    # Suffix match: any subdomain is bypassed.
    assert options.bypasses_proxy("api.example.com") is True
    # Exact match of the entry itself.
    assert options.bypasses_proxy("example.com") is True
    # Case-insensitive.
    assert options.bypasses_proxy("API.EXAMPLE.COM") is True
    # A host that merely ends with the same characters but is not a
    # dot-delimited suffix must NOT be bypassed.
    assert options.bypasses_proxy("notexample.com") is False
    assert options.bypasses_proxy("example.org") is False


def test_bypasses_proxy_leading_dot_entry() -> None:
    """A leading-dot ``.example.com`` entry strips the dot and matches the suffix."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=(".example.com",),
    )
    assert options.bypasses_proxy("api.example.com") is True
    assert options.bypasses_proxy("example.com") is True
    assert options.bypasses_proxy("notexample.com") is False


def test_bypasses_proxy_ignores_port_on_candidate_and_entry() -> None:
    """Conventional NO_PROXY parity: ports on the candidate or entry are ignored."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("example.com:443",),
    )
    # A port on the candidate host is stripped before matching.
    assert options.bypasses_proxy("api.example.com:443") is True
    assert options.bypasses_proxy("example.com:8443") is True
    # A port on the entry itself is ignored (host-only suffix match).
    assert options.bypasses_proxy("api.example.com") is True
    # No spurious match through the port handling.
    assert options.bypasses_proxy("notexample.com:443") is False


def test_bypasses_proxy_ipv6_literal_with_port() -> None:
    """A bracketed IPv6 literal still matches once a port is appended."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("::1",),
    )
    # Bare IPv6 (multiple colons, no port) is left intact and matches exactly.
    assert options.bypasses_proxy("::1") is True
    # Bracketed form with a port drops both brackets and port before matching.
    assert options.bypasses_proxy("[::1]:443") is True


def test_bypasses_proxy_explicit_glob_still_works() -> None:
    """Explicit ``*`` globs keep their fnmatch semantics."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        non_proxy_hosts=("*.example.com",),
    )
    assert options.bypasses_proxy("api.example.com") is True
    assert options.bypasses_proxy("a.b.example.com") is True
    # A bare ``*.example.com`` glob does not match the apex domain.
    assert options.bypasses_proxy("example.com") is False


def test_repr_masks_credentials() -> None:
    """``repr`` masks both username and password when present."""
    options = ProxyOptions(
        type=ProxyType.HTTP,
        host="proxy.corp",
        port=8080,
        username="alice",
        password="s3cret",
    )
    rendered = repr(options)
    assert "***" in rendered
    assert "alice" not in rendered
    assert "s3cret" not in rendered


def test_repr_omits_mask_when_no_creds() -> None:
    """``repr`` does not include ``'***'`` when credentials are absent."""
    options = ProxyOptions(type=ProxyType.HTTP, host="proxy.corp", port=8080)
    assert "***" not in repr(options)


def test_from_configuration_https_proxy_url() -> None:
    """A full HTTPS_PROXY URL parses into all ``ProxyOptions`` fields."""
    config = (
        Configuration.builder()
        .put(Configuration.HTTPS_PROXY, "http://user:pw@proxy.corp:8080")
        .build()
    )
    options = ProxyOptions.from_configuration(config)
    assert options is not None
    assert options.type is ProxyType.HTTP
    assert options.host == "proxy.corp"
    assert options.port == 8080
    assert options.username == "user"
    assert options.password == "pw"


def test_from_configuration_https_wins_over_http() -> None:
    """When both env vars are set, HTTPS_PROXY takes precedence."""
    config = (
        Configuration.builder()
        .put(Configuration.HTTPS_PROXY, "http://secure.proxy:8443")
        .put(Configuration.HTTP_PROXY, "http://plain.proxy:8080")
        .build()
    )
    options = ProxyOptions.from_configuration(config)
    assert options is not None
    assert options.host == "secure.proxy"
    assert options.port == 8443


def test_from_configuration_no_proxy_wildcard() -> None:
    """``NO_PROXY=*`` short-circuits to ``None``."""
    config = (
        Configuration.builder()
        .put(Configuration.HTTPS_PROXY, "http://proxy.corp:8080")
        .put(Configuration.NO_PROXY, "*")
        .build()
    )
    assert ProxyOptions.from_configuration(config) is None


def test_from_configuration_no_proxy_list() -> None:
    """``NO_PROXY`` is a comma-separated list copied into ``non_proxy_hosts``."""
    config = (
        Configuration.builder()
        .put(Configuration.HTTPS_PROXY, "http://proxy.corp:8080")
        .put(Configuration.NO_PROXY, "example.com,*.internal")
        .build()
    )
    options = ProxyOptions.from_configuration(config)
    assert options is not None
    assert options.non_proxy_hosts == ("example.com", "*.internal")


def test_from_configuration_hostless_url_returns_none() -> None:
    """A URL with a scheme but no host parses to ``None`` rather than raising."""
    config = Configuration.builder().put(Configuration.HTTPS_PROXY, "http://").build()
    assert ProxyOptions.from_configuration(config) is None


def test_from_configuration_invalid_port_returns_none() -> None:
    """An out-of-range port in the URL yields ``None``."""
    config = (
        Configuration.builder().put(Configuration.HTTPS_PROXY, "http://proxy.corp:99999").build()
    )
    assert ProxyOptions.from_configuration(config) is None


def test_from_configuration_no_env_returns_none() -> None:
    """An empty configuration produces ``None``."""
    config = Configuration(overrides={}, env=lambda _name: None)
    assert ProxyOptions.from_configuration(config) is None


def _config(url: str) -> Configuration:
    """Build a configuration with only ``HTTPS_PROXY`` set."""
    return Configuration.builder().put(Configuration.HTTPS_PROXY, url).build()


class TestFromConfigurationScheme:
    """The proxy URL scheme selects the transport flavour (L4)."""

    def test_socks5_scheme_maps_to_socks5_type(self) -> None:
        options = ProxyOptions.from_configuration(_config("socks5://proxy.corp:1080"))
        assert options is not None
        assert options.type is ProxyType.SOCKS5

    def test_socks5h_scheme_maps_to_socks5_type(self) -> None:
        options = ProxyOptions.from_configuration(_config("socks5h://proxy.corp:1080"))
        assert options is not None
        assert options.type is ProxyType.SOCKS5

    def test_socks4_scheme_maps_to_socks4_type(self) -> None:
        options = ProxyOptions.from_configuration(_config("socks4://proxy.corp:1080"))
        assert options is not None
        assert options.type is ProxyType.SOCKS4

    def test_http_scheme_maps_to_http_type(self) -> None:
        options = ProxyOptions.from_configuration(_config("http://proxy.corp:8080"))
        assert options is not None
        assert options.type is ProxyType.HTTP

    def test_unsupported_scheme_rejected_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An unsupported scheme must be rejected, NOT silently downgraded to
        # HTTP, and the rejection must be visible above DEBUG.
        with caplog.at_level("WARNING", logger="dexpace.sdk.core.util.proxy"):
            options = ProxyOptions.from_configuration(_config("ftp://proxy.corp:8080"))
        assert options is None
        assert any(record.levelname == "WARNING" for record in caplog.records)


class TestFromConfigurationPortDefaults:
    """A missing port defaults by scheme instead of dropping the proxy (L4)."""

    def test_http_without_port_defaults_to_80(self) -> None:
        options = ProxyOptions.from_configuration(_config("http://proxy.corp"))
        assert options is not None
        assert options.port == 80

    def test_https_without_port_defaults_to_443(self) -> None:
        options = ProxyOptions.from_configuration(_config("https://proxy.corp"))
        assert options is not None
        assert options.port == 443

    def test_socks_without_port_defaults_to_1080(self) -> None:
        options = ProxyOptions.from_configuration(_config("socks5://proxy.corp"))
        assert options is not None
        assert options.port == 1080


class TestFromConfigurationSchemeless:
    """A scheme-less ``host:port`` form is parsed as an HTTP proxy (L4)."""

    def test_schemeless_host_port(self) -> None:
        options = ProxyOptions.from_configuration(_config("proxy.corp:8080"))
        assert options is not None
        assert options.type is ProxyType.HTTP
        assert options.host == "proxy.corp"
        assert options.port == 8080

    def test_schemeless_host_only_defaults_port(self) -> None:
        options = ProxyOptions.from_configuration(_config("proxy.corp"))
        assert options is not None
        assert options.host == "proxy.corp"
        assert options.port == 80


class TestFromConfigurationCredentials:
    """Percent-encoded proxy credentials are ``unquote()``-decoded (L4)."""

    def test_percent_encoded_credentials_decoded(self) -> None:
        # user "u@s" and password "p:w" percent-encode their reserved chars.
        options = ProxyOptions.from_configuration(_config("http://u%40s:p%3Aw@proxy.corp:8080"))
        assert options is not None
        assert options.username == "u@s"
        assert options.password == "p:w"


class TestFromConfigurationVisibility:
    """A genuinely unusable proxy config is logged above DEBUG (L4)."""

    def test_hostless_url_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="dexpace.sdk.core.util.proxy"):
            options = ProxyOptions.from_configuration(_config("http://"))
        assert options is None
        assert any(record.levelname == "WARNING" for record in caplog.records)


class TestBypassGlobWithPort:
    """Glob bypass patterns strip a trailing ``:port`` so host:port matches (IMP12)."""

    def test_ported_glob_matches_port_qualified_candidate(self) -> None:
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("*.example.com:443",),
        )
        # Before the fix the ``:443`` baked into the compiled regex meant the
        # pattern never matched a (port-stripped) candidate.
        assert options.bypasses_proxy("api.example.com") is True
        assert options.bypasses_proxy("api.example.com:443") is True
        assert options.bypasses_proxy("api.example.com:8443") is True

    def test_ported_glob_does_not_over_match(self) -> None:
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("*.example.com:443",),
        )
        assert options.bypasses_proxy("api.example.org") is False
