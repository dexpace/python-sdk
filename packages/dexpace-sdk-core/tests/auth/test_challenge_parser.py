# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``WWW-Authenticate`` challenge parser."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.auth import AuthenticateChallenge, parse_challenges


class TestParseChallenges:
    def test_parse_single_basic_challenge(self) -> None:
        challenges = parse_challenges('Basic realm="example"')
        assert len(challenges) == 1
        assert challenges[0].scheme == "Basic"
        assert challenges[0].parameters == {"realm": "example"}

    def test_parse_multiple_challenges(self) -> None:
        challenges = parse_challenges('Basic realm="r1", Digest realm="r2", qop="auth"')
        assert len(challenges) == 2
        assert challenges[0].scheme == "Basic"
        assert challenges[0].parameters == {"realm": "r1"}
        assert challenges[1].scheme == "Digest"
        assert challenges[1].parameters == {"realm": "r2", "qop": "auth"}

    def test_parse_quoted_pair(self) -> None:
        # quoted-pair: backslash followed by a char decodes to the bare char
        challenges = parse_challenges(r'Digest realm="he said \"hi\"", nonce="a\\b"')
        assert len(challenges) == 1
        assert challenges[0].parameters["realm"] == 'he said "hi"'
        assert challenges[0].parameters["nonce"] == "a\\b"

    def test_parse_case_insensitive_param_names(self) -> None:
        challenges = parse_challenges('Digest REALM="x", QoP="auth"')
        assert challenges[0].parameters["realm"] == "x"
        assert challenges[0].parameters["qop"] == "auth"

    @pytest.mark.req("AUTH-13")
    def test_parse_malformed_skips_gracefully(self) -> None:
        # Garbage between two valid challenges should not break parsing.
        challenges = parse_challenges('Basic realm="ok", =badtoken, Digest realm="d"')
        schemes = [c.scheme for c in challenges]
        assert "Basic" in schemes
        assert "Digest" in schemes

    @pytest.mark.req("AUTH-13")
    def test_parse_empty_returns_empty(self) -> None:
        assert parse_challenges("") == []
        assert parse_challenges("   ") == []

    def test_parse_unquoted_token_value(self) -> None:
        challenges = parse_challenges("Digest algorithm=SHA-256, qop=auth")
        assert challenges[0].parameters == {"algorithm": "SHA-256", "qop": "auth"}

    def test_parse_comma_inside_quoted_string(self) -> None:
        challenges = parse_challenges('Digest realm="a, b", qop="auth"')
        assert len(challenges) == 1
        assert challenges[0].parameters["realm"] == "a, b"
        assert challenges[0].parameters["qop"] == "auth"

    def test_authenticate_challenge_is_frozen(self) -> None:
        c = AuthenticateChallenge(scheme="Basic", parameters={"realm": "x"})
        import dataclasses

        try:
            c.scheme = "Digest"  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected FrozenInstanceError")


class TestQuotedCommaBattery:
    """Commas inside quoted strings must never split a challenge or param."""

    def test_quoted_comma_in_first_of_two_challenges(self) -> None:
        # The comma inside the Digest ``domain`` quoted-string must not be
        # mistaken for the boundary before the trailing Basic challenge.
        challenges = parse_challenges(
            'Digest realm="r", domain="/a, /b", qop="auth", Basic realm="b"'
        )
        assert [c.scheme for c in challenges] == ["Digest", "Basic"]
        assert challenges[0].parameters["domain"] == "/a, /b"
        assert challenges[0].parameters["qop"] == "auth"
        assert challenges[1].parameters["realm"] == "b"

    def test_multiple_commas_inside_one_quoted_value(self) -> None:
        challenges = parse_challenges('Digest realm="a, b, c, d", nonce="x"')
        assert len(challenges) == 1
        assert challenges[0].parameters["realm"] == "a, b, c, d"
        assert challenges[0].parameters["nonce"] == "x"

    def test_escaped_quote_then_comma_inside_value(self) -> None:
        # An escaped quote must not prematurely close the string, so the comma
        # that follows is still protected.
        challenges = parse_challenges(r'Digest realm="he said \"a, b\"", qop="auth"')
        assert len(challenges) == 1
        assert challenges[0].parameters["realm"] == 'he said "a, b"'
        assert challenges[0].parameters["qop"] == "auth"


class TestMultiHeaderLine:
    """Challenges split across several ``WWW-Authenticate`` header lines.

    RFC 7230 §3.2.2 lets a server emit repeated header lines; they are
    semantically one comma-joined field. ``parse_challenges`` accepts the raw
    lines as an iterable and parses each independently, concatenating the
    result — so a Digest challenge on its own line is never dropped in favour
    of a Basic challenge on the first line.
    """

    def test_two_lines_basic_then_digest(self) -> None:
        lines = ['Basic realm="a"', 'Digest realm="b", qop="auth", nonce="n"']
        challenges = parse_challenges(lines)
        assert [c.scheme for c in challenges] == ["Basic", "Digest"]
        assert challenges[0].parameters == {"realm": "a"}
        assert challenges[1].parameters == {"realm": "b", "qop": "auth", "nonce": "n"}

    def test_each_line_may_carry_multiple_challenges(self) -> None:
        lines = [
            'Basic realm="a", Digest realm="d1", algorithm=MD5',
            'Digest realm="d2", algorithm=SHA-256',
        ]
        challenges = parse_challenges(lines)
        assert [c.scheme for c in challenges] == ["Basic", "Digest", "Digest"]
        assert challenges[1].parameters["algorithm"] == "MD5"
        assert challenges[2].parameters["algorithm"] == "SHA-256"

    def test_quoted_comma_survives_per_line_parsing(self) -> None:
        lines = ['Digest realm="a, b", qop="auth"', 'Basic realm="c"']
        challenges = parse_challenges(lines)
        assert challenges[0].parameters["realm"] == "a, b"
        assert [c.scheme for c in challenges] == ["Digest", "Basic"]

    def test_empty_iterable_returns_empty(self) -> None:
        assert parse_challenges([]) == []

    def test_single_line_iterable_matches_string_form(self) -> None:
        text = 'Basic realm="r1", Digest realm="r2", qop="auth"'
        assert parse_challenges([text]) == parse_challenges(text)
