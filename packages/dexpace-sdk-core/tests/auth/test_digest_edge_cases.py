# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Edge-case tests for the Digest challenge handler.

Covers the decline-on-unencodable-credentials path, the RFC 2069 (qop-less)
header shape, and the preference tuple acting as an algorithm allow-list.
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
_PASSWORD = "Circle of Life"
_REALM = "http-auth@example.org"
_NONCE = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
_CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
_URL = Url(scheme="https", host="example.org", path="/dir/index.html")


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


class TestUnencodableCredentials:
    def test_non_latin1_creds_against_charsetless_challenge_declines(self) -> None:
        # No ``charset`` directive -> ISO-8859-1 default, which cannot encode
        # a CJK password. ``handle`` must decline (return None), not raise.
        handler = DigestChallengeHandler(
            "ユーザー",
            "パスワード",
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "qop": "auth", "nonce": _NONCE, "algorithm": "MD5"},
        )
        assert handler.handle(Method.GET, _URL, [challenge], is_proxy=False) is None

    def test_non_latin1_creds_succeed_when_utf8_advertised(self) -> None:
        # With ``charset=UTF-8`` the same credentials encode fine.
        handler = DigestChallengeHandler(
            "ユーザー",
            "パスワード",
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={
                "realm": _REALM,
                "qop": "auth",
                "nonce": _NONCE,
                "algorithm": "MD5",
                "charset": "UTF-8",
            },
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None


class TestQopLessHeader:
    def test_qopless_header_omits_cnonce_and_nc(self) -> None:
        # RFC 2069: server omits ``qop``. The response header must not carry
        # ``cnonce`` or ``nc`` per RFC 7616 §3.4.
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "nonce": _NONCE, "algorithm": "MD5"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        _, value = result
        params = _parse_auth(value)
        assert "cnonce" not in params
        assert "nc" not in params
        assert "qop" not in params
        # The RFC 2069 response digest is still emitted.
        assert "response" in params

    def test_qop_present_still_emits_cnonce_and_nc(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "qop": "auth", "nonce": _NONCE, "algorithm": "MD5"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        params = _parse_auth(result[1])
        assert params["cnonce"] == _CNONCE
        assert params["nc"] == "00000001"
        assert params["qop"] == "auth"


class TestSessionVariantQopLess:
    def test_sess_algorithm_without_qop_declines(self) -> None:
        # A ``*-sess`` algorithm folds ``cnonce`` into HA1, but a qop-less
        # response omits ``cnonce``/``nc`` — leaving the server unable to
        # reconstruct HA1. The handler declines rather than emit an
        # unverifiable header.
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "nonce": _NONCE, "algorithm": "SHA-256-sess"},
        )
        assert handler.handle(Method.GET, _URL, [challenge], is_proxy=False) is None

    def test_sess_algorithm_with_qop_still_works(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            cnonce_factory=lambda: _CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={
                "realm": _REALM,
                "qop": "auth",
                "nonce": _NONCE,
                "algorithm": "SHA-256-sess",
            },
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        params = _parse_auth(result[1])
        assert params["cnonce"] == _CNONCE
        assert params["algorithm"] == "SHA-256-sess"


class TestPreferenceAllowList:
    def test_sha256_preference_declines_md5_only_server(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("SHA-256",),
            cnonce_factory=lambda: _CNONCE,
        )
        md5_only = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "qop": "auth", "nonce": _NONCE, "algorithm": "MD5"},
        )
        assert handler.handle(Method.GET, _URL, [md5_only], is_proxy=False) is None
        assert handler.can_handle([md5_only]) is False

    def test_default_preference_still_reaches_md5(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            cnonce_factory=lambda: _CNONCE,
        )
        md5_only = AuthenticateChallenge(
            scheme="Digest",
            parameters={"realm": _REALM, "qop": "auth", "nonce": _NONCE, "algorithm": "MD5"},
        )
        result = handler.handle(Method.GET, _URL, [md5_only], is_proxy=False)
        assert result is not None
        assert _parse_auth(result[1])["algorithm"] == "MD5"
