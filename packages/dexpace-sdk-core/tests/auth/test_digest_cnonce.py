# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests proving the Digest client nonce (``cnonce``) uses a CSPRNG.

RFC 7616 leaves ``cnonce`` generation to the client, but a predictable value
undermines the protection ``qop=auth`` is meant to provide: an attacker who can
guess the ``cnonce`` can precompute or replay responses. The default factory
must therefore draw from ``secrets`` (a cryptographically secure source), never
the ``random`` module (a Mersenne-Twister PRNG that is trivially predictable
once a little output is observed).

These checks are structural: the module must not import ``random``, and the
generated ``cnonce`` must look like fresh high-entropy hex on every call.
"""

from __future__ import annotations

import re

from dexpace.sdk.core.http.auth import AuthenticateChallenge, DigestChallengeHandler
from dexpace.sdk.core.http.auth import digest as digest_module
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request.method import Method

_REALM = "http-auth@example.org"
_NONCE = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
_URL = Url(scheme="https", host="example.org", path="/dir/index.html")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _cnonce_from_default_factory() -> str:
    # Exercise the real default factory (no ``cnonce_factory`` override) and
    # read back the value it stamped into the header.
    handler = DigestChallengeHandler("Mufasa", "Circle of Life")
    challenge = AuthenticateChallenge(
        scheme="Digest",
        parameters={"realm": _REALM, "qop": "auth", "nonce": _NONCE, "algorithm": "MD5"},
    )
    result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
    assert result is not None
    match = re.search(r'cnonce="([^"]*)"', result[1])
    assert match is not None
    return match.group(1)


class TestCnonceCsprng:
    def test_module_imports_secrets_not_random(self) -> None:
        # ``import secrets`` binds the name in the module namespace; the absence
        # of ``random`` proves the insecure PRNG was never pulled in.
        assert getattr(digest_module, "secrets", None) is not None
        assert getattr(digest_module, "random", None) is None

    def test_default_cnonce_is_128_bit_hex(self) -> None:
        # ``secrets.token_hex(16)`` yields 16 CSPRNG bytes rendered as 32 lower
        # hex digits — 128 bits of entropy.
        cnonce = _cnonce_from_default_factory()
        assert _HEX32.match(cnonce)

    def test_default_cnonce_differs_across_calls(self) -> None:
        # A deterministic factory would repeat; a CSPRNG effectively never does.
        seen = {_cnonce_from_default_factory() for _ in range(16)}
        assert len(seen) == 16
