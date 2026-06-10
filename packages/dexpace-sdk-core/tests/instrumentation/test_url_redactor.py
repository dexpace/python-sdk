# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``UrlRedactor``."""

from __future__ import annotations

from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.instrumentation import UrlRedactor


def test_strips_userinfo() -> None:
    redactor = UrlRedactor()
    redacted = redactor.redact("https://user:secret@api.example.com/path")
    assert "user" not in redacted
    assert "secret" not in redacted
    assert "api.example.com" in redacted


def test_allowlisted_query_unredacted() -> None:
    redactor = UrlRedactor()
    redacted = redactor.redact("https://api.example.com/v1?api-version=1.0&token=hunter2")
    assert "api-version=1.0" in redacted
    # Non-allowlisted params collapse to a canonical REDACTED=REDACTED so the
    # parameter name ("token") never leaks either.
    assert "REDACTED=REDACTED" in redacted
    assert "token" not in redacted
    assert "hunter2" not in redacted


def test_accepts_parsed_url() -> None:
    redactor = UrlRedactor()
    parsed = Url.parse("https://api.example.com/v1?secret=value")
    out = redactor.redact(parsed)
    assert "REDACTED=REDACTED" in out
    assert "secret" not in out
    assert "value" not in out


def test_unparseable_input_fails_closed() -> None:
    # A URL that cannot be parsed must never reach the log verbatim: it may
    # embed a secret. The redactor fails closed to a constant placeholder.
    redactor = UrlRedactor()
    assert redactor.redact("not a url") == "REDACTED:unparseable"


def test_unparseable_secret_does_not_leak() -> None:
    redactor = UrlRedactor()
    secret = "Bearer sk-live-abc123def456"
    out = redactor.redact(f"://{secret}")
    assert secret not in out
    assert out == "REDACTED:unparseable"


def test_invalid_port_fails_closed() -> None:
    # furl raises ValueError("Invalid port ...") for a non-numeric port; the
    # redactor catches it and fails closed rather than crashing the log path.
    redactor = UrlRedactor()
    assert redactor.redact("http://host:notaport/path") == "REDACTED:unparseable"


def test_invalid_ipv6_fails_closed() -> None:
    # An unterminated IPv6 literal makes furl raise ValueError; fail closed.
    redactor = UrlRedactor()
    assert redactor.redact("http://[bad") == "REDACTED:unparseable"


def test_custom_allowlist() -> None:
    redactor = UrlRedactor(allowed_query_keys={"plain"})
    out = redactor.redact("https://example.com/?plain=ok&api-version=1.0")
    assert "plain=ok" in out
    # api-version was not in the custom allow-list; key and value are redacted.
    assert "REDACTED=REDACTED" in out
    assert "api-version" not in out


def test_multiple_values_per_key() -> None:
    redactor = UrlRedactor(allowed_query_keys=set())
    out = redactor.redact("https://example.com/?a=1&a=2")
    assert out.count("REDACTED=REDACTED") == 2
    assert "a=" not in out


def test_bare_token_query_redacts_key_and_value() -> None:
    # A bare token with no '=' parses as a key with an empty value. Without
    # key redaction the secret would survive verbatim as the parameter name.
    redactor = UrlRedactor()
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    out = redactor.redact(f"https://api.example.com/v1?{token}")
    assert token not in out
    assert "REDACTED=REDACTED" in out


def test_redactor_redacts_fragment_by_default() -> None:
    redactor = UrlRedactor()
    out = redactor.redact("https://x/api?api-version=1#token=abc")
    assert "abc" not in out


def test_redactor_redacts_path_when_enabled() -> None:
    redactor = UrlRedactor(redact_path=True)
    out = redactor.redact("https://x/users/123/secret/abc")
    assert "abc" not in out and "123" not in out


def test_fragment_preserved_when_redact_fragment_false() -> None:
    redactor = UrlRedactor(redact_fragment=False)
    out = redactor.redact("https://x/api?api-version=1#token=abc")
    assert "token=abc" in out


def test_path_preserved_by_default() -> None:
    redactor = UrlRedactor()
    out = redactor.redact("https://x/users/123/secret/abc")
    assert "/users/123/secret/abc" in out
