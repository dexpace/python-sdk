# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for Digest ``qop`` negotiation and the ``auth-int`` safety guard.

``auth-int`` folds a hash of the request entity body into ``HA2`` (RFC 7616
§3.4.3). The ``ChallengeHandler`` contract carries no request body, and a
single-use (non-replayable) body may already be consumed by the time a 401
challenge is answered — so an ``auth-int`` response cannot be computed here.

The handler therefore prefers ``qop=auth`` whenever the server offers it and
declines an ``auth-int``-only challenge rather than emit a bogus header. The
underlying digest routine refuses ``auth-int`` loudly so no code path can ever
silently fall through and hash the request as if it were RFC 2069.
"""

from __future__ import annotations

import re

import pytest

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
    for part in re.split(r",\s*(?=[a-zA-Z][a-zA-Z0-9_-]*=)", body):
        key, _, raw = part.partition("=")
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        out[key.strip().lower()] = raw
    return out


def _handler() -> DigestChallengeHandler:
    return DigestChallengeHandler(
        _USERNAME,
        _PASSWORD,
        preferred_algorithms=("MD5",),
        cnonce_factory=lambda: _CNONCE,
    )


class TestQopNegotiation:
    def test_prefers_auth_when_both_auth_and_auth_int_offered(self) -> None:
        # A server offering ``qop="auth,auth-int"`` must be answered with
        # ``auth`` — the only protection this handler can compute.
        handler = _handler()
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={
                "realm": _REALM,
                "qop": "auth,auth-int",
                "nonce": _NONCE,
                "algorithm": "MD5",
            },
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        assert _parse_auth(result[1])["qop"] == "auth"

    def test_prefers_auth_when_auth_int_listed_first(self) -> None:
        # Ordering within the ``qop`` list must not matter — ``auth`` wins even
        # when the server lists ``auth-int`` ahead of it.
        handler = _handler()
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={
                "realm": _REALM,
                "qop": "auth-int, auth",
                "nonce": _NONCE,
                "algorithm": "MD5",
            },
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        assert _parse_auth(result[1])["qop"] == "auth"

    @pytest.mark.req("AUTH-15")
    def test_auth_int_only_challenge_declines(self) -> None:
        # ``auth-int`` alone cannot be satisfied (no body available), so the
        # handler declines rather than emitting an unverifiable header.
        handler = _handler()
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={
                "realm": _REALM,
                "qop": "auth-int",
                "nonce": _NONCE,
                "algorithm": "MD5",
            },
        )
        assert handler.handle(Method.GET, _URL, [challenge], is_proxy=False) is None


class TestAuthIntFailsLoudly:
    def test_compute_response_rejects_auth_int(self) -> None:
        # Defence in depth: if ``auth-int`` ever reaches the digest routine it
        # must raise loudly, never fall through to the RFC 2069 branch and
        # silently emit a hash the server will reject.
        import hashlib

        handler = _handler()
        with pytest.raises(ValueError, match="auth-int"):
            handler._compute_response(
                hasher=hashlib.md5,
                algorithm="MD5",
                method="GET",
                uri="/dir/index.html",
                realm=_REALM,
                nonce=_NONCE,
                nc="00000001",
                cnonce=_CNONCE,
                qop="auth-int",
                charset="iso-8859-1",
            )
