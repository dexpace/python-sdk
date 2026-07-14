# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the Digest challenge handler (RFC 7616)."""

from __future__ import annotations

import re

from dexpace.sdk.core.http.auth import (
    AuthenticateChallenge,
    BasicChallengeHandler,
    ChallengeHandler,
    CompositeChallengeHandler,
    DigestChallengeHandler,
)
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request.method import Method

# RFC 7616 §3.9.1 example
_RFC_PARAMS = {
    "realm": "http-auth@example.org",
    "qop": "auth",
    "nonce": "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
    "opaque": "FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS",
}
_USERNAME = "Mufasa"
_PASSWORD = "Circle of Life"
_FIXED_CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
_URL = Url(scheme="https", host="example.org", path="/dir/index.html")


def _parse_auth(value: str) -> dict[str, str]:
    assert value.startswith("Digest ")
    body = value[len("Digest ") :]
    out: dict[str, str] = {}
    # Naive splitter — sufficient for parsing our own outputs.
    parts = re.split(r",\s*(?=[a-zA-Z][a-zA-Z0-9_-]*=)", body)
    for part in parts:
        k, _, v = part.partition("=")
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k.strip().lower()] = v
    return out


class TestDigestChallengeHandler:
    def test_digest_md5_known_vector(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        name, value = result
        assert name == "Authorization"
        params = _parse_auth(value)
        assert params["response"] == "8ca523f5e9506fed4657c9700eebdbec"
        assert params["username"] == "Mufasa"
        assert params["realm"] == "http-auth@example.org"
        assert params["uri"] == "/dir/index.html"
        assert params["nc"] == "00000001"
        assert params["cnonce"] == _FIXED_CNONCE
        assert params["qop"] == "auth"
        assert params["algorithm"] == "MD5"
        assert params["opaque"] == _RFC_PARAMS["opaque"]

    def test_digest_sha256_known_vector(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("SHA-256",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "SHA-256"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        _, value = result
        params = _parse_auth(value)
        assert params["response"] == (
            "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"
        )
        assert params["algorithm"] == "SHA-256"

    def test_digest_prefers_sha256_when_both_offered(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        md5 = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5"},
        )
        sha = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "SHA-256"},
        )
        result = handler.handle(Method.GET, _URL, [md5, sha], is_proxy=False)
        assert result is not None
        _, value = result
        params = _parse_auth(value)
        assert params["algorithm"] == "SHA-256"

    def test_digest_nonce_counter_increments_on_reuse(self) -> None:
        # Reusing the same server nonce across requests increments ``nc``
        # (RFC 7616 §3.4: count of requests sent with this nonce).
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5"},
        )
        first = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        second = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert first is not None and second is not None
        assert _parse_auth(first[1])["nc"] == "00000001"
        assert _parse_auth(second[1])["nc"] == "00000002"

    def test_digest_nc_resets_for_new_nonce(self) -> None:
        # A fresh server nonce restarts ``nc`` at 00000001 (RFC 7616 §3.4),
        # even after a prior nonce advanced the count. A single global counter
        # would wrongly emit 00000003 here.
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        first_nonce = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5", "nonce": "nonce-aaa"},
        )
        second_nonce = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5", "nonce": "nonce-bbb"},
        )
        # Advance the count on the first nonce.
        handler.handle(Method.GET, _URL, [first_nonce], is_proxy=False)
        second = handler.handle(Method.GET, _URL, [first_nonce], is_proxy=False)
        assert second is not None
        assert _parse_auth(second[1])["nc"] == "00000002"
        # A different nonce must reset to 00000001.
        fresh = handler.handle(Method.GET, _URL, [second_nonce], is_proxy=False)
        assert fresh is not None
        assert _parse_auth(fresh[1])["nc"] == "00000001"

    def test_digest_nc_resumes_per_nonce_when_alternating(self) -> None:
        # Each nonce keeps its own independent count: alternating between two
        # nonces must resume each one's count rather than share a global one.
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )

        def nc_for(nonce: str) -> str:
            challenge = AuthenticateChallenge(
                scheme="Digest",
                parameters={**_RFC_PARAMS, "algorithm": "MD5", "nonce": nonce},
            )
            result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
            assert result is not None
            return _parse_auth(result[1])["nc"]

        assert nc_for("nonce-aaa") == "00000001"
        assert nc_for("nonce-bbb") == "00000001"
        assert nc_for("nonce-aaa") == "00000002"
        assert nc_for("nonce-bbb") == "00000002"
        assert nc_for("nonce-aaa") == "00000003"

    def test_digest_nonce_count_map_is_bounded(self) -> None:
        # A long-lived handler hitting many distinct nonces must not grow the
        # per-nonce map without bound; the oldest entry is evicted past the cap.
        from dexpace.sdk.core.http.auth.digest import _MAX_TRACKED_NONCES

        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        for index in range(_MAX_TRACKED_NONCES + 50):
            challenge = AuthenticateChallenge(
                scheme="Digest",
                parameters={**_RFC_PARAMS, "algorithm": "MD5", "nonce": f"nonce-{index}"},
            )
            handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert len(handler._nonce_counts) == _MAX_TRACKED_NONCES

    def test_digest_is_proxy_returns_proxy_authorization_header(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=True)
        assert result is not None
        name, value = result
        assert name == "Proxy-Authorization"
        assert value.startswith("Digest ")

    def test_digest_no_handlable_challenge_returns_none(self) -> None:
        handler = DigestChallengeHandler(_USERNAME, _PASSWORD)
        basic = AuthenticateChallenge(scheme="Basic", parameters={"realm": "r"})
        assert handler.handle(Method.GET, _URL, [basic], is_proxy=False) is None
        assert handler.can_handle([basic]) is False

    def test_digest_md5_sess_uses_session_ha1(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5-sess",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5-sess"},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        params = _parse_auth(result[1])
        assert params["algorithm"] == "MD5-sess"

    def test_digest_url_uses_path_and_query(self) -> None:
        from dexpace.sdk.core.http.common.url import QueryParams

        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5"},
        )
        url = Url(
            scheme="https",
            host="example.org",
            path="/dir/index.html",
            query=QueryParams((("a", "1"),)),
        )
        result = handler.handle(Method.GET, url, [challenge], is_proxy=False)
        assert result is not None
        params = _parse_auth(result[1])
        assert params["uri"] == "/dir/index.html?a=1"


class TestBasicChallengeHandler:
    def test_handles_basic_challenge(self) -> None:
        handler = BasicChallengeHandler("user", "pass")
        challenge = AuthenticateChallenge(scheme="Basic", parameters={"realm": "r"})
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result == ("Authorization", "Basic dXNlcjpwYXNz")

    def test_returns_none_for_non_basic(self) -> None:
        handler = BasicChallengeHandler("user", "pass")
        challenge = AuthenticateChallenge(scheme="Digest", parameters={})
        assert handler.handle(Method.GET, _URL, [challenge], is_proxy=False) is None

    def test_is_proxy_emits_proxy_authorization(self) -> None:
        handler = BasicChallengeHandler("user", "pass")
        challenge = AuthenticateChallenge(scheme="Basic", parameters={"realm": "r"})
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=True)
        assert result is not None
        assert result[0] == "Proxy-Authorization"


class TestCompositeChallengeHandler:
    def test_first_matching_handler_wins(self) -> None:
        basic = BasicChallengeHandler("u", "p")
        digest = DigestChallengeHandler("u", "p")
        composite = CompositeChallengeHandler(digest, basic)
        challenges = [
            AuthenticateChallenge(scheme="Basic", parameters={"realm": "r"}),
        ]
        result = composite.handle(Method.GET, _URL, challenges, is_proxy=False)
        # digest can't handle, basic can — composite must fall through.
        assert result == ("Authorization", "Basic dTpw")

    def test_returns_none_when_no_handler_matches(self) -> None:
        composite = CompositeChallengeHandler(
            DigestChallengeHandler("u", "p"),
            BasicChallengeHandler("u", "p"),
        )
        challenges = [AuthenticateChallenge(scheme="Bearer", parameters={})]
        assert composite.handle(Method.GET, _URL, challenges, is_proxy=False) is None

    def test_can_handle_delegates_to_children(self) -> None:
        composite = CompositeChallengeHandler(BasicChallengeHandler("u", "p"))
        assert composite.can_handle([AuthenticateChallenge(scheme="Basic", parameters={})])
        assert not composite.can_handle([AuthenticateChallenge(scheme="Bearer", parameters={})])

    def test_prefers_strongest_scheme_when_all_offered(self) -> None:
        # A server offering Basic + Digest/MD5 + Digest/SHA-256 must be answered
        # with the strongest the composite supports: Digest SHA-256. Ordering
        # Digest ahead of Basic in the composite makes Digest win over Basic,
        # and Digest's own preference makes SHA-256 win over MD5.
        composite = CompositeChallengeHandler(
            DigestChallengeHandler(_USERNAME, _PASSWORD, cnonce_factory=lambda: _FIXED_CNONCE),
            BasicChallengeHandler(_USERNAME, _PASSWORD),
        )
        challenges = [
            AuthenticateChallenge(scheme="Basic", parameters={"realm": _RFC_PARAMS["realm"]}),
            AuthenticateChallenge(scheme="Digest", parameters={**_RFC_PARAMS, "algorithm": "MD5"}),
            AuthenticateChallenge(
                scheme="Digest", parameters={**_RFC_PARAMS, "algorithm": "SHA-256"}
            ),
        ]
        result = composite.handle(Method.GET, _URL, challenges, is_proxy=False)
        assert result is not None
        name, value = result
        assert name == "Authorization"
        assert value.startswith("Digest ")
        assert _parse_auth(value)["algorithm"] == "SHA-256"


class TestRfc7616Vectors:
    """End-to-end reproduction of the RFC 7616 §3.9.1 worked example.

    The example authenticates ``GET /dir/index.html`` for user ``Mufasa`` with
    password ``Circle of Life`` against realm ``http-auth@example.org``, using
    the fixed ``nonce``/``cnonce``/``opaque`` the RFC pins. The SHA-256
    ``response`` digest is quoted verbatim from the RFC; the MD5 digest is the
    widely-published value for the same inputs.
    """

    def _handle(self, algorithm: str) -> dict[str, str]:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=(algorithm,),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": algorithm},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        return _parse_auth(result[1])

    def test_sha256_full_header_matches_rfc(self) -> None:
        params = self._handle("SHA-256")
        assert params == {
            "username": "Mufasa",
            "realm": "http-auth@example.org",
            "uri": "/dir/index.html",
            "algorithm": "SHA-256",
            "nonce": _RFC_PARAMS["nonce"],
            "nc": "00000001",
            "cnonce": _FIXED_CNONCE,
            "qop": "auth",
            "response": ("753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"),
            "opaque": _RFC_PARAMS["opaque"],
        }

    def test_md5_full_header_matches_known_vector(self) -> None:
        params = self._handle("MD5")
        assert params["algorithm"] == "MD5"
        assert params["response"] == "8ca523f5e9506fed4657c9700eebdbec"
        assert params["nc"] == "00000001"
        assert params["cnonce"] == _FIXED_CNONCE
        assert params["opaque"] == _RFC_PARAMS["opaque"]


class TestNonceCountRobustness:
    """``nc`` is strictly monotonic per nonce and the tracking map is bounded."""

    def _nc_for(self, handler: DigestChallengeHandler, nonce: str) -> int:
        challenge = AuthenticateChallenge(
            scheme="Digest",
            parameters={**_RFC_PARAMS, "algorithm": "MD5", "nonce": nonce},
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        return int(_parse_auth(result[1])["nc"], 16)

    def test_nc_is_strictly_monotonic_over_many_reuses(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        previous = 0
        for expected in range(1, 5001):
            current = self._nc_for(handler, "stable-nonce")
            assert current == expected
            assert current > previous
            previous = current

    def test_nc_renders_as_eight_lowercase_hex_digits(self) -> None:
        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        challenge = AuthenticateChallenge(
            scheme="Digest", parameters={**_RFC_PARAMS, "algorithm": "MD5"}
        )
        result = handler.handle(Method.GET, _URL, [challenge], is_proxy=False)
        assert result is not None
        assert re.fullmatch(r"[0-9a-f]{8}", _parse_auth(result[1])["nc"])

    def test_actively_reused_nonce_survives_eviction_pressure(self) -> None:
        # Interleaving a hot nonce with a flood of one-shot nonces must not
        # evict the hot nonce: each reuse refreshes its recency, so its count
        # keeps climbing rather than resetting to 1.
        from dexpace.sdk.core.http.auth.digest import _MAX_TRACKED_NONCES

        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        hot_uses = 0
        for index in range(_MAX_TRACKED_NONCES * 2):
            self._nc_for(handler, "hot")
            hot_uses += 1
            self._nc_for(handler, f"cold-{index}")
        assert self._nc_for(handler, "hot") == hot_uses + 1
        assert len(handler._nonce_counts) == _MAX_TRACKED_NONCES

    def test_map_stays_bounded_under_pure_rotation(self) -> None:
        from dexpace.sdk.core.http.auth.digest import _MAX_TRACKED_NONCES

        handler = DigestChallengeHandler(
            _USERNAME,
            _PASSWORD,
            preferred_algorithms=("MD5",),
            cnonce_factory=lambda: _FIXED_CNONCE,
        )
        for index in range(_MAX_TRACKED_NONCES * 3):
            self._nc_for(handler, f"rotating-{index}")
            assert len(handler._nonce_counts) <= _MAX_TRACKED_NONCES
        assert len(handler._nonce_counts) == _MAX_TRACKED_NONCES


def _structural_protocol_check() -> ChallengeHandler:
    """Compile-time check that BasicChallengeHandler is a ChallengeHandler."""
    return BasicChallengeHandler("u", "p")
