# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``HeaderRedactor`` — header-name gating and URL-valued redaction."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.instrumentation import (
    DEFAULT_LOGGED_HEADER_ALLOWLIST,
    DEFAULT_URL_VALUED_HEADERS,
    HeaderRedactor,
)


def _headers(*pairs: tuple[str, str]) -> Headers:
    return Headers.outbound(list(pairs))


@pytest.mark.req("OBS-17")
def test_url_valued_response_headers_are_redacted() -> None:
    # Location and Content-Location values are routed through the URL-value
    # redactor, so a secret in the query never reaches the log.
    redactor = HeaderRedactor()
    out = redactor.redact(
        _headers(
            ("Location", "https://idp.example.com/cb?code=SEKRET&api-version=1"),
            ("Content-Location", "/mirror?signature=SEKRET"),
        )
    )
    assert "SEKRET" not in out["location"]
    assert "REDACTED=REDACTED" in out["location"]
    assert "api-version=1" in out["location"]
    assert out["content-location"] == "/mirror?***"


@pytest.mark.req("OBS-17")
def test_non_url_allowlisted_header_passes_through_unchanged() -> None:
    out = HeaderRedactor().redact(_headers(("Content-Type", "application/json; charset=utf-8")))
    assert out["content-type"] == "application/json; charset=utf-8"


@pytest.mark.req("OBS-17")
def test_url_valued_names_are_configurable() -> None:
    # A custom URL-valued name is redacted; the defaults still apply too.
    redactor = HeaderRedactor(
        allowed_names={"x-next-url", "location"},
        url_valued_names={"x-next-url"},
    )
    out = redactor.redact(_headers(("X-Next-Url", "/next?token=SEKRET")))
    assert out["x-next-url"] == "/next?***"


@pytest.mark.req("OBS-18")
def test_disallowed_header_marked_redacted_by_default() -> None:
    # A non-allowlisted (credential) name has its value replaced by the fixed
    # marker; its value is never logged.
    out = HeaderRedactor().redact(_headers(("Authorization", "Bearer super-secret-token")))
    assert out["authorization"] == "REDACTED"
    assert "super-secret-token" not in out["authorization"]


@pytest.mark.req("OBS-18")
def test_disallowed_header_omitted_when_configured() -> None:
    out = HeaderRedactor(redact_disallowed=False).redact(
        _headers(("Authorization", "Bearer super-secret-token"), ("Date", "now"))
    )
    assert "authorization" not in out  # omitted entirely
    assert out["date"] == "now"  # allow-listed name still logged


@pytest.mark.req("OBS-18")
def test_default_allowlist_is_diagnostic_and_non_credential() -> None:
    # Only diagnostic, non-credential names are logged by default.
    for credential in (
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "www-authenticate",
        "proxy-authenticate",
    ):
        assert credential not in DEFAULT_LOGGED_HEADER_ALLOWLIST
    for diagnostic in ("content-length", "content-type", "date", "location", "content-location"):
        assert diagnostic in DEFAULT_LOGGED_HEADER_ALLOWLIST
    # URL-valued defaults must themselves be allow-listed so they are logged.
    assert DEFAULT_URL_VALUED_HEADERS <= DEFAULT_LOGGED_HEADER_ALLOWLIST


@pytest.mark.req("OBS-18")
def test_name_matching_is_case_insensitive_and_values_joined() -> None:
    # Header names arrive lower-cased; a multi-valued header is joined.
    out = HeaderRedactor().redact(_headers(("Via", "1.1 a"), ("Via", "1.1 b")))
    assert out["via"] == "1.1 a, 1.1 b"
