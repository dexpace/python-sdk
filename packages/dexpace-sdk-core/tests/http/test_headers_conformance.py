# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance battery for ``Headers``: canonical fold, multimap, dual paths.

Certifies the behaviours required of an HTTP header model:

* lower-case canonical storage with ASCII-invariant, locale-independent folding;
* case-folding lookup, membership, and equality;
* ordered multi-values (the ``get_all`` equivalent);
* removal via a "set to no values" call;
* outbound-strict vs inbound-lenient obs-text handling, kept distinct;
* rejection of CR / LF / NUL header injection on every path;
* error messages that never echo header *values* and escape control characters
  in header *names*.

``hypothesis`` is not a dev dependency of this workspace, so the fold-idempotence
and equality-iff-fold-equality properties are exercised with a seeded, generative
casing sweep that stands in for a property-based test.
"""

from __future__ import annotations

import random
import string

import pytest

from dexpace.sdk.core.http.common import Headers, HttpHeaderName
from dexpace.sdk.core.http.common.http_header_name import CONTENT_TYPE, X_REQUEST_ID

# Valid RFC 7230 token names used across the generative sweeps.
_NAMES = (
    "content-type",
    "x-request-id",
    "accept",
    "cache-control",
    "x-custom-header",
    "etag",
    "via",
    "www-authenticate",
)

# A representative obs-text value: bytes 0x80-0xFF decoded as Latin-1, exactly
# what a transport hands back when a server sends a non-ASCII header value.
_OBS_TEXT = "caf\xe9-\x80-\xff"

_INJECTIONS = ("v\r\nLocation: evil", "v\nsplit", "v\0nul")

# Non-ASCII names whose naive ``str.lower()`` would either fold to an ASCII
# token (Kelvin sign) or expand via locale-independent special-casing (dotted /
# dotless I). Written as escapes so the source stays pure ASCII (no RUF001).
_NON_ASCII_NAMES = (
    "\u212a-header",  # U+212A KELVIN SIGN: str.lower() folds it to "k"
    "\u0130-header",  # U+0130 dotted capital I -> "i" + combining mark
    "\u0131-header",  # U+0131 dotless small i (the Turkish trap)
    "caf\xe9",  # any non-ASCII letter
)


def _random_casing(name: str, rng: random.Random) -> str:
    """Return ``name`` with each ASCII letter independently upper/lower-cased."""
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in name)


class TestCanonicalFoldStorage:
    def test_names_stored_lower_and_iteration_yields_canonical(self) -> None:
        rng = random.Random(1)
        for base in _NAMES:
            for _ in range(20):
                cased = _random_casing(base, rng)
                h = Headers([(cased, "v")])
                assert h.names() == (base,)
                assert list(h) == [base]
                assert h.get(cased.upper()) == "v"

    def test_fold_is_idempotent(self) -> None:
        rng = random.Random(2)
        for base in _NAMES:
            for _ in range(20):
                cased = _random_casing(base, rng)
                once = Headers([(cased, "v")]).names()[0]
                twice = Headers([(once, "v")]).names()[0]
                assert once == twice == base

    def test_fold_is_ascii_and_locale_invariant(self) -> None:
        # Every ASCII letter folds to its a-z counterpart regardless of casing.
        for letter in string.ascii_letters:
            stored = Headers([(f"x-{letter}", "v")]).names()[0]
            assert stored == f"x-{letter.lower()}"
            assert stored.isascii()


class TestEqualityFollowsFold:
    def test_equal_iff_names_fold_equal(self) -> None:
        rng = random.Random(3)
        for base in _NAMES:
            for _ in range(30):
                a = Headers([(_random_casing(base, rng), "v")])
                b = Headers([(_random_casing(base, rng), "v")])
                # Same fold + same value -> equal and hash-consistent.
                assert a == b
                assert hash(a) == hash(b)

    def test_distinct_names_not_equal(self) -> None:
        a = Headers([("content-type", "v")])
        b = Headers([("x-request-id", "v")])
        assert a != b

    def test_same_fold_distinct_value_not_equal(self) -> None:
        a = Headers([("Content-Type", "a")])
        b = Headers([("content-type", "b")])
        assert a != b
        assert a.names() == b.names()


class TestOrderedMultiValues:
    def test_get_all_equivalent_preserves_order(self) -> None:
        h = Headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Set-Cookie", "c=3")])
        assert h.values("set-cookie") == ("a=1", "b=2", "c=3")

    def test_distinct_names_keep_insertion_order(self) -> None:
        h = Headers([("B", "1"), ("A", "2"), ("C", "3")])
        assert h.names() == ("b", "a", "c")


class TestRemovalBySettingNoValues:
    def test_with_set_no_values_removes_header(self) -> None:
        h = Headers([("A", "1"), ("B", "2")])
        result = h.with_set("a")
        assert "a" not in result
        assert result.names() == ("b",)

    def test_with_set_no_values_is_noop_when_absent(self) -> None:
        h = Headers([("A", "1")])
        assert h.with_set("missing") is h

    def test_outbound_with_set_no_values_removes_header(self) -> None:
        h = Headers.outbound([("A", "1"), ("B", "2")])
        assert h.with_set("B").names() == ("a",)


class TestInboundLenientObsText:
    def test_default_constructor_preserves_obs_text(self) -> None:
        h = Headers([("X-Obs", _OBS_TEXT)])
        assert h.get("x-obs") == _OBS_TEXT

    def test_mutators_preserve_obs_text(self) -> None:
        h = Headers().with_added("X-Obs", _OBS_TEXT).with_set("X-Other", _OBS_TEXT)
        assert h.values("x-obs") == (_OBS_TEXT,)
        assert h.values("x-other") == (_OBS_TEXT,)

    @pytest.mark.parametrize("value", _INJECTIONS)
    def test_inbound_still_rejects_crlf_injection(self, value: str) -> None:
        # Lenient about obs-text, but never about the injection-critical bytes.
        with pytest.raises(ValueError, match="invalid header value"):
            Headers([("X-Test", value)])


class TestOutboundStrictObsText:
    def test_outbound_accepts_clean_ascii(self) -> None:
        h = Headers.outbound([("Content-Type", "application/json")])
        assert h.get("content-type") == "application/json"

    def test_outbound_rejects_obs_text(self) -> None:
        with pytest.raises(ValueError, match="obs-text"):
            Headers.outbound([("X-Obs", _OBS_TEXT)])

    @pytest.mark.parametrize("value", ["v\x01ctl", "v\x7fdel", "v\x1fus"])
    def test_outbound_rejects_other_control_bytes(self, value: str) -> None:
        with pytest.raises(ValueError, match="invalid header value"):
            Headers.outbound([("X-Test", value)])

    @pytest.mark.parametrize("value", _INJECTIONS)
    def test_outbound_rejects_crlf_injection(self, value: str) -> None:
        with pytest.raises(ValueError, match="invalid header value"):
            Headers.outbound([("X-Test", value)])

    def test_paths_diverge_only_on_obs_text(self) -> None:
        # The exact value the lenient path accepts is what the strict path bars.
        assert Headers([("X-Obs", _OBS_TEXT)]).get("x-obs") == _OBS_TEXT
        with pytest.raises(ValueError):
            Headers.outbound([("X-Obs", _OBS_TEXT)])


class TestErrorMessageHygiene:
    def test_value_error_never_echoes_the_value(self) -> None:
        secret = "topsecret-token\r\nSet-Cookie: sid=evil"
        with pytest.raises(ValueError) as exc:
            Headers([("Authorization", secret)])
        assert "topsecret-token" not in str(exc.value)

    def test_strict_value_error_never_echoes_the_value(self) -> None:
        with pytest.raises(ValueError) as exc:
            Headers.outbound([("X-Test", "secret\x80payload")])
        message = str(exc.value)
        assert "secret" not in message
        assert "payload" not in message

    def test_name_error_escapes_control_characters(self) -> None:
        with pytest.raises(ValueError) as exc:
            Headers([("X-\r\nInjected", "v")])
        message = str(exc.value)
        # No raw control bytes leak into the message; they appear escaped.
        assert "\r" not in message
        assert "\n" not in message
        assert "\\r" in message
        assert "\\n" in message


class TestAsciiInvariantFolding:
    @pytest.mark.parametrize("name", _NON_ASCII_NAMES)
    def test_non_ascii_names_rejected_not_folded(self, name: str) -> None:
        # A locale-sensitive ``str.lower()`` before validation would let some of
        # these masquerade as ASCII tokens; validating the original rejects them.
        with pytest.raises(ValueError, match="invalid header name"):
            Headers([(name, "v")])

    def test_ascii_capital_i_folds_to_plain_i(self) -> None:
        # Never to a dotless-i; the fold is ASCII, not Turkish-locale.
        assert Headers([("I-HEADER", "v")]).names() == ("i-header",)


class TestHttpHeaderNameHotPath:
    def test_constant_key_stored_as_lower(self) -> None:
        h = Headers([(CONTENT_TYPE, "application/json")])
        assert h.names() == ("content-type",)
        assert CONTENT_TYPE in h

    def test_constant_preserves_wire_casing(self) -> None:
        # The stored key folds, but the constant keeps its wire form for output.
        assert CONTENT_TYPE.value == "content-type"
        assert CONTENT_TYPE.canonical_name == "Content-Type"
        assert str(CONTENT_TYPE) == "Content-Type"
        assert str(X_REQUEST_ID) == "X-Request-Id"

    def test_constant_bypasses_token_validation(self) -> None:
        # An HttpHeaderName is trusted: its precomputed ``value`` is used verbatim
        # (skipping the ``.lower()`` and token checks the raw-string path runs).
        trusted = HttpHeaderName(value="x-trusted", canonical_name="X-Trusted")
        assert Headers([(trusted, "v")]).get(trusted) == "v"

    def test_outbound_accepts_constant_names(self) -> None:
        h = Headers.outbound([(CONTENT_TYPE, "text/plain")])
        assert h.get(CONTENT_TYPE) == "text/plain"
