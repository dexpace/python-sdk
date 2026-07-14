# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the `dexpace.sdk.core.util.proxy` module."""

from __future__ import annotations

import os

import pytest

from dexpace.sdk.core.config.configuration import Configuration
from dexpace.sdk.core.util import ProxyOptions, ProxyType


@pytest.mark.req("CFG-22")
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


@pytest.mark.req("CFG-23")
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


@pytest.mark.req("CFG-23")
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
    """A full HTTPS_PROXY URL parses into all ``ProxyOptions`` fields.

    Credentials survive because the proxy connection itself is ``https``
    (encrypted); see ``TestFromConfigurationCredentials`` for the plaintext
    contrast where they are dropped.
    """
    config = (
        Configuration.builder()
        .put(Configuration.HTTPS_PROXY, "https://user:pw@proxy.corp:8080")
        .build()
    )
    options = ProxyOptions.from_configuration(config)
    assert options is not None
    assert options.type is ProxyType.HTTP
    assert options.host == "proxy.corp"
    assert options.port == 8080
    assert options.username == "user"
    assert options.password == "pw"


@pytest.mark.req("CFG-24")
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


@pytest.mark.req("CFG-27")
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


class TestFromConfigurationPortUnspecified:
    """A missing port stays ``None`` — never a fabricated per-scheme default.

    Keeping the proxy (rather than dropping a port-less URL) is required, but
    the model must not invent a port: the transport decides its own default at
    connection time. The port comes from the URL or is ``None``, nothing else.
    """

    def test_http_without_port_stays_none(self) -> None:
        options = ProxyOptions.from_configuration(_config("http://proxy.corp"))
        assert options is not None
        assert options.port is None

    def test_https_without_port_stays_none(self) -> None:
        options = ProxyOptions.from_configuration(_config("https://proxy.corp"))
        assert options is not None
        assert options.port is None

    def test_socks_without_port_stays_none(self) -> None:
        options = ProxyOptions.from_configuration(_config("socks5://proxy.corp"))
        assert options is not None
        assert options.port is None

    def test_explicit_in_range_port_is_kept(self) -> None:
        options = ProxyOptions.from_configuration(_config("http://proxy.corp:3128"))
        assert options is not None
        assert options.port == 3128


class TestFromConfigurationSchemeless:
    """A scheme-less ``host:port`` form is parsed as an HTTP proxy (L4)."""

    def test_schemeless_host_port(self) -> None:
        options = ProxyOptions.from_configuration(_config("proxy.corp:8080"))
        assert options is not None
        assert options.type is ProxyType.HTTP
        assert options.host == "proxy.corp"
        assert options.port == 8080

    def test_schemeless_host_only_leaves_port_none(self) -> None:
        options = ProxyOptions.from_configuration(_config("proxy.corp"))
        assert options is not None
        assert options.host == "proxy.corp"
        assert options.port is None


class TestFromConfigurationCredentials:
    """Credentials attach only over an ``https`` proxy, and are decoded."""

    def test_percent_encoded_credentials_decoded_over_https(self) -> None:
        # user "u@s" and password "p:w" percent-encode their reserved chars;
        # the https proxy scheme keeps them (encrypted proxy connection).
        options = ProxyOptions.from_configuration(_config("https://u%40s:p%3Aw@proxy.corp:8080"))
        assert options is not None
        assert options.username == "u@s"
        assert options.password == "p:w"

    def test_credentials_dropped_over_http_proxy(self) -> None:
        # A plaintext (http) proxy connection must never carry a credential:
        # Proxy-Authorization would travel in the clear, so it is dropped.
        options = ProxyOptions.from_configuration(_config("http://user:pw@proxy.corp:8080"))
        assert options is not None
        assert options.host == "proxy.corp"
        assert options.username is None
        assert options.password is None

    def test_credentials_dropped_over_schemeless_proxy(self) -> None:
        # A scheme-less proxy is assumed plaintext HTTP, so credentials drop.
        options = ProxyOptions.from_configuration(_config("user:pw@proxy.corp:8080"))
        assert options is not None
        assert options.host == "proxy.corp"
        assert options.username is None
        assert options.password is None

    def test_credentials_dropped_over_socks_proxy(self) -> None:
        # SOCKS proxy auth is likewise unencrypted; drop the credential.
        options = ProxyOptions.from_configuration(_config("socks5://user:pw@proxy.corp:1080"))
        assert options is not None
        assert options.username is None
        assert options.password is None


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


_SECRET = "hunter2-super-secret"


class TestStringFormsMaskCredentials:
    """Every string rendering of a proxy masks its credentials (never leaks)."""

    def _with_creds(self) -> ProxyOptions:
        return ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            username="alice",
            password=_SECRET,
        )

    def test_repr_masks_password(self) -> None:
        rendered = repr(self._with_creds())
        assert "***" in rendered
        assert _SECRET not in rendered
        assert "alice" not in rendered

    @pytest.mark.req("CFG-22")
    def test_str_masks_password(self) -> None:
        # ``str`` must mask too — it is a distinct string form from ``repr``.
        rendered = str(self._with_creds())
        assert "***" in rendered
        assert _SECRET not in rendered
        assert "alice" not in rendered

    def test_format_masks_password(self) -> None:
        # f-string / ``format`` route through ``__str__``/``__format__``.
        rendered = f"{self._with_creds()}"
        assert _SECRET not in rendered

    def test_no_string_form_leaks_the_literal_secret(self) -> None:
        # Grep every string rendering for the literal secret: zero hits.
        options = self._with_creds()
        for rendered in (repr(options), str(options), f"{options}", format(options)):
            assert _SECRET not in rendered


class TestBypassAllShortCircuit:
    """A single bare ``*`` bypass entry short-circuits to bypass-everything."""

    @pytest.mark.req("CFG-22")
    def test_bare_star_bypasses_every_host(self) -> None:
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("*",),
        )
        assert options.bypasses_proxy("example.com") is True
        assert options.bypasses_proxy("anything.internal") is True
        assert options.bypasses_proxy("10.0.0.1") is True
        assert options.bypasses_proxy("") is True

    @pytest.mark.req("CFG-23", "CFG-27")
    def test_bare_star_short_circuits_without_running_matchers(self) -> None:
        # "Don't process ``*`` as a literal glob needing host matching": the
        # bare-``*`` entry is not compiled into a per-host matcher at all.
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("*",),
        )
        assert options._bypass_all is True
        assert options._bypass_matchers == ()

    def test_star_mixed_with_other_entries_still_bypasses_all(self) -> None:
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("example.com", "*", "*.internal"),
        )
        assert options._bypass_all is True
        assert options.bypasses_proxy("totally.unrelated.host") is True


class TestSameLayerHostAndPort:
    """Host and port always come from the SAME configuration layer, never mixed."""

    def test_higher_layer_supplies_both_host_and_port(self) -> None:
        # The env layer offers one endpoint and the (higher-priority) override
        # layer another. The winning layer supplies BOTH host and port — you
        # never observe the override host paired with the env port or vice
        # versa, the classic mix-and-match footgun.
        env = {"HTTPS_PROXY": "http://env-host:1111"}
        config = (
            Configuration.builder()
            .env(env.get)
            .put(Configuration.HTTPS_PROXY, "http://override-host:2222")
            .build()
        )
        options = ProxyOptions.from_configuration(config)
        assert options is not None
        assert options.host == "override-host"
        assert options.port == 2222

    @pytest.mark.req("CFG-24")
    def test_single_url_ties_host_and_port_together(self) -> None:
        # Both parts derive from one resolved URL string, so a port-less URL
        # yields the URL's host and a ``None`` port — never a port borrowed
        # from some other source.
        config = _config("https://only-host")
        options = ProxyOptions.from_configuration(config)
        assert options is not None
        assert options.host == "only-host"
        assert options.port is None


_PROXY_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})


def _forbid_proxy_env_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booby-trap ``os.environ.get`` so any proxy-variable read raises.

    Only the ``get`` accessor is shadowed (item assignment and the rest of the
    mapping keep working, so the test harness is unaffected). Unrelated reads
    delegate to the real environment; a read of any proxy variable raises,
    proving the code under test never resolves proxy settings from
    ``os.environ`` implicitly.
    """
    real_get = os.environ.get

    def guarded_get(key: str, default: str | None = None) -> str | None:
        if key in _PROXY_ENV_KEYS:
            raise AssertionError(f"proxy env var {key!r} must not be read implicitly")
        return real_get(key, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)


class TestOptInEnvironmentResolution:
    """Env resolution is opt-in and hermetic — never an implicit os.environ read."""

    @pytest.mark.req("CFG-28")
    def test_plain_construction_never_reads_proxy_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With proxy-env reads booby-trapped, build and exercise the model
        # directly. Constructing and matching must not touch proxy env vars.
        _forbid_proxy_env_reads(monkeypatch)
        options = ProxyOptions(
            type=ProxyType.HTTP,
            host="proxy.corp",
            port=8080,
            non_proxy_hosts=("*.internal", "example.com"),
            username="u",
            password="p",
        )
        assert options.bypasses_proxy("api.internal") is True
        assert options.bypasses_proxy("elsewhere.org") is False

    @pytest.mark.req("CFG-28")
    def test_from_environment_uses_injected_seam_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Opt-in env resolution reads ONLY the injected seam; the real
        # os.environ is booby-trapped to prove it is never consulted, and no
        # real environment variable is mutated by the test.
        _forbid_proxy_env_reads(monkeypatch)
        fake_env = {"HTTPS_PROXY": "http://seam-host:8080", "NO_PROXY": "example.com"}
        options = ProxyOptions.from_environment(env=fake_env.get)
        assert options is not None
        assert options.host == "seam-host"
        assert options.port == 8080
        assert options.non_proxy_hosts == ("example.com",)

    def test_from_environment_returns_none_when_seam_is_empty(self) -> None:
        options = ProxyOptions.from_environment(env=lambda _name: None)
        assert options is None

    def test_from_environment_default_reads_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default seam IS os.environ.get, so the explicit opt-in call (and
        # only it) reflects the real process environment when no seam is given.
        monkeypatch.setenv("HTTPS_PROXY", "http://env-default:8080")
        monkeypatch.delenv("NO_PROXY", raising=False)
        options = ProxyOptions.from_environment()
        assert options is not None
        assert options.host == "env-default"
        assert options.port == 8080


class TestResolutionNeverThrows:
    """Malformed proxy configuration degrades to ``None`` — it never raises."""

    @pytest.mark.req("CFG-24")
    @pytest.mark.parametrize(
        "value",
        [
            "http://",  # no host
            "http://proxy.corp:99999",  # port out of range
            "ftp://proxy.corp:8080",  # unsupported scheme
            "://::::",  # garbage
            "http://[unterminated",  # malformed IPv6 authority
            "   ",  # whitespace only
            "not a url at all",  # a host may never contain whitespace
        ],
    )
    def test_malformed_value_degrades_to_none(self, value: str) -> None:
        # No exception escapes for any malformed value; it degrades to None.
        assert ProxyOptions.from_configuration(_config(value)) is None


class TestLoggingRedactsCredentials:
    """The WARNING log path for an unusable proxy never leaks credentials."""

    def test_invalid_port_url_redacts_userinfo(self, caplog: pytest.LogCaptureFixture) -> None:
        secret_url = f"http://alice:{_SECRET}@proxy.corp:99999"
        with caplog.at_level("WARNING", logger="dexpace.sdk.core.util.proxy"):
            options = ProxyOptions.from_configuration(_config(secret_url))
        assert options is None
        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert blob  # a WARNING was emitted
        assert _SECRET not in blob
        assert "alice" not in blob
        assert "***@proxy.corp" in blob
