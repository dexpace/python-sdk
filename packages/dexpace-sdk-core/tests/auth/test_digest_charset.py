# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for Digest credential-charset selection (RFC 7616 §3.4).

The ``charset`` directive controls how ``username`` and ``password`` are
encoded before hashing. ``charset=UTF-8`` selects UTF-8; its absence (or any
other value) falls back to the legacy ISO-8859-1 default. For a password with
a non-ASCII character the two encodings yield distinct ``response`` digests,
so the chosen branch is observable end to end.
"""

from __future__ import annotations

import re

from dexpace.sdk.core.http.auth import (
    AuthenticateChallenge,
    DigestChallengeHandler,
)
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request.method import Method

_USERNAME = "Mufasa"
# ``é`` (U+00E9) encodes to one byte in ISO-8859-1 and two in UTF-8.
_PASSWORD = "Circle of Lifé"
_REALM = "http-auth@example.org"
_NONCE = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
_CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
_URL = Url(scheme="https", host="example.org", path="/dir/index.html")

# Reference MD5/qop=auth responses computed for ``_PASSWORD`` under each codec.
_RESPONSE_UTF8 = "9c931f7ef105acbcc1e9f99ba923d170"
_RESPONSE_ISO_8859_1 = "0d97ea03337d88fc75698b9ef88d349d"


def _parse_auth(value: str) -> dict[str, str]:
    assert value.startswith("Digest ")
    body = value[len("Digest ") :]
    out: dict[str, str] = {}
    parts = re.split(r",\s*(?=[a-zA-Z][a-zA-Z0-9_-]*=)", body)
    for part in parts:
        key, _, raw = part.partition("=")
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        out[key.strip().lower()] = raw
    return out


def _handle(parameters: dict[str, str]) -> dict[str, str]:
    handler = DigestChallengeHandler(
        _USERNAME,
        _PASSWORD,
        preferred_algorithms=("MD5",),
        cnonce_factory=lambda: _CNONCE,
    )
    challenge = AuthenticateChallenge(scheme="Digest", parameters=parameters)
    result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
    assert result is not None
    _, value = result
    return _parse_auth(value)


def _base_params() -> dict[str, str]:
    return {
        "realm": _REALM,
        "qop": "auth",
        "nonce": _NONCE,
        "algorithm": "MD5",
    }


def test_uses_utf8_when_charset_advertised() -> None:
    params = {**_base_params(), "charset": "UTF-8"}

    parsed = _handle(params)

    assert parsed["response"] == _RESPONSE_UTF8


def test_charset_directive_is_case_insensitive() -> None:
    params = {**_base_params(), "charset": "utf-8"}

    parsed = _handle(params)

    assert parsed["response"] == _RESPONSE_UTF8


def test_uses_iso_8859_1_when_charset_absent() -> None:
    parsed = _handle(_base_params())

    assert parsed["response"] == _RESPONSE_ISO_8859_1


def test_uses_iso_8859_1_for_unrecognised_charset() -> None:
    params = {**_base_params(), "charset": "US-ASCII"}

    parsed = _handle(params)

    assert parsed["response"] == _RESPONSE_ISO_8859_1


def test_charset_branches_diverge_for_non_ascii_secret() -> None:
    with_utf8 = _handle({**_base_params(), "charset": "UTF-8"})
    without = _handle(_base_params())

    assert with_utf8["response"] != without["response"]
