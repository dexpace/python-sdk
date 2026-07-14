# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Self-consistency checks for the golden wire-exactness byte corpus.

This suite proves the corpus loads and is internally coherent — it does NOT
implement the production codecs/parsers that will later consume the fixtures.
Where a vector claims a relationship (strict render / lenient parse inverse,
canonical date round-trip, ``Retry-After`` classification), the check
re-derives it with a small stdlib oracle so a corrupted or mis-authored
fixture fails the build. The oracles here are fixture validators, not the SDK's
URL codec, redirect resolver, pagination splicer, SSE parser, or retry-pacing
parser — those live elsewhere and point their own conformance at this corpus.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from email.utils import formatdate, parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import pytest

from .golden_corpus import (
    FAMILIES,
    FAMILY_TAGS,
    load_all,
    load_pagination_splice,
    load_query_codec,
    load_redirect,
    load_retry_pacing,
    load_rfc1123_dates,
    load_sse,
)

# ----- stdlib fixture-validation oracles (NOT the production codecs) -------

_PRESCREEN = re.compile(r"^\s*\d+(\.\d+)?\s*$")


def _strict_render(params: tuple[tuple[bytes, bytes], ...]) -> str:
    """Strict RFC 3986 query render: unreserved-only, space->%20, +->%2B."""
    return "&".join(
        f"{quote(name.decode(), safe='')}={quote(value.decode(), safe='')}"
        for name, value in params
    )


def _lenient_parse(query: str) -> list[tuple[str, str]]:
    """Lenient RFC 3986 query parse: '+' stays literal (never form-decoded)."""
    if not query:
        return []
    pairs: list[tuple[str, str]] = []
    for token in query.split("&"):
        name, sep, value = token.partition("=")
        pairs.append((unquote(name), unquote(value) if sep else ""))
    return pairs


def _resolve(base: str, location: str) -> str:
    """Raw-component Location splice: urljoin then drop userinfo."""
    parts = urlsplit(urljoin(base, location))
    netloc = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _parsedate_or_none(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _float_ok(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


# ----- corpus-wide structure ----------------------------------------------


def test_family_tags_cover_exactly_the_families() -> None:
    assert set(FAMILY_TAGS) == set(FAMILIES)
    assert len(set(FAMILY_TAGS.values())) == len(FAMILIES)


def test_every_family_loads_and_is_nonempty() -> None:
    corpus = load_all()
    assert set(corpus) == set(FAMILIES)
    for family in FAMILIES:
        vectors = corpus[family]
        assert vectors, f"{family} has no vectors"
        for vector in vectors:
            assert vector.family == family
            assert vector.tag == FAMILY_TAGS[family]
            assert vector.id
            assert vector.description


def test_vector_ids_are_unique_within_each_family() -> None:
    for family, vectors in load_all().items():
        ids = [vector.id for vector in vectors]
        assert len(ids) == len(set(ids)), f"duplicate id in {family}"


def test_wire_fields_load_as_bytes_never_str() -> None:
    for query in load_query_codec():
        assert isinstance(query.query, bytes)
        assert all(isinstance(n, bytes) and isinstance(v, bytes) for n, v in query.params)
    for redirect in load_redirect():
        assert isinstance(redirect.base, bytes)
        assert isinstance(redirect.location, bytes)
        assert isinstance(redirect.resolved, bytes)
    for splice in load_pagination_splice():
        assert isinstance(splice.base, bytes) and isinstance(splice.spliced, bytes)
    for sse in load_sse():
        assert isinstance(sse.stream, bytes)
    for pacing in load_retry_pacing():
        assert isinstance(pacing.raw, bytes)
    for date in load_rfc1123_dates():
        assert isinstance(date.text, bytes)


# ----- query codec (HTTP-29) ----------------------------------------------


@pytest.mark.req("HTTP-29")
def test_query_codec_render_and_parse_match_claims() -> None:
    for vector in load_query_codec():
        canonical = [(n.decode(), v.decode()) for n, v in vector.params]
        query = vector.query.decode("ascii")
        if vector.render_matches:
            assert _strict_render(vector.params) == query, vector.id
        if vector.parse_matches:
            assert _lenient_parse(query) == canonical, vector.id
        assert vector.round_trippable == (vector.render_matches and vector.parse_matches)


def test_query_codec_round_trip_is_exact_inverse() -> None:
    for vector in load_query_codec():
        if not vector.round_trippable:
            continue
        canonical = [(n.decode(), v.decode()) for n, v in vector.params]
        assert _lenient_parse(_strict_render(vector.params)) == canonical, vector.id


def test_query_codec_pins_named_edge_cases() -> None:
    by_id = {vector.id: vector for vector in load_query_codec()}
    assert by_id["space-encodes-to-pct20"].query == b"q=a%20b"
    assert by_id["plus-encodes-to-pct2b"].query == b"q=a%2Bb"
    assert by_id["valueless-flag-empty-string"].query == b"flag="
    # A literal wire '+' parses back to '+', not space (not round-trippable).
    literal_plus = by_id["parse-literal-plus-not-space"]
    assert literal_plus.query == b"q=a+b"
    assert literal_plus.parse_matches and not literal_plus.render_matches
    # A bare flag is a present empty string, distinct from absent.
    assert by_id["parse-bare-flag-is-empty"].params == ((b"flag", b""),)


# ----- redirect (REDIR-12) ------------------------------------------------


@pytest.mark.req("REDIR-12")
def test_redirect_resolution_matches_raw_splice() -> None:
    for vector in load_redirect():
        expected = _resolve(vector.base.decode(), vector.location.decode())
        assert vector.resolved.decode() == expected, vector.id


def test_redirect_drops_userinfo() -> None:
    for vector in load_redirect():
        authority = vector.resolved.split(b"://", 1)[1].split(b"/", 1)[0]
        assert b"@" not in authority, vector.id


def test_redirect_keeps_ipv6_brackets_and_percent_encoding() -> None:
    by_id = {vector.id: vector for vector in load_redirect()}
    for vid in ("ipv6-brackets-kept-absolute", "ipv6-brackets-kept-relative"):
        assert b"[" in by_id[vid].resolved and b"]" in by_id[vid].resolved
    encoded = by_id["preserve-percent-encoding"].resolved
    assert b"%2F" in encoded and b"%20" in encoded  # never decoded/normalised
    assert b"user:pass" not in by_id["userinfo-dropped"].resolved


# ----- pagination splice (PAGE-21) ----------------------------------------


@pytest.mark.req("PAGE-21")
def test_pagination_splice_adds_param_and_preserves_untouched() -> None:
    for vector in load_pagination_splice():
        assert vector.param + b"=" + vector.value in vector.spliced, vector.id
        for untouched in vector.untouched:
            assert untouched in vector.spliced, (vector.id, untouched)


def test_pagination_splice_preserves_bytes_verbatim() -> None:
    by_id = {vector.id: vector for vector in load_pagination_splice()}
    # A literal '+' is neither re-encoded to %2B nor decoded to space.
    assert b"tag=a+b" in by_id["preserve-literal-plus-verbatim"].spliced
    # A pre-encoded %20 survives byte-for-byte.
    assert b"q=a%20b" in by_id["preserve-encoded-param-verbatim"].spliced


# ----- SSE (SSE-14) -------------------------------------------------------


@pytest.mark.req("SSE-14")
def test_sse_streams_preserve_declared_terminators() -> None:
    for vector in load_sse():
        assert vector.stream, vector.id
        stream = vector.stream
        if vector.terminator == "CRLF":
            assert b"\r\n" in stream, vector.id
        elif vector.terminator == "CR":
            bare_cr = any(
                byte == 0x0D and (i + 1 >= len(stream) or stream[i + 1] != 0x0A)
                for i, byte in enumerate(stream)
            )
            assert bare_cr, vector.id
        elif vector.terminator == "LF":
            assert b"\n" in stream and b"\r" not in stream, vector.id
        else:
            pytest.fail(f"unknown terminator {vector.terminator!r} in {vector.id}")


def test_sse_expected_events_are_well_formed() -> None:
    for vector in load_sse():
        assert vector.events, vector.id
        for event in vector.events:
            assert isinstance(event.data, str) and isinstance(event.event, str)
            assert event.id is None or isinstance(event.id, str)
            assert event.retry is None or isinstance(event.retry, int)


def test_sse_binary_sensitive_fixtures_are_byte_exact() -> None:
    by_id = {vector.id: vector for vector in load_sse()}
    assert by_id["crlf-single-event"].stream == b"data: hello\r\n\r\n"
    assert by_id["cr-single-event"].stream == b"data: hello\r\r"
    assert by_id["leading-bom-stripped"].stream.startswith(b"\xef\xbb\xbf")
    assert by_id["utf8-data-payload"].stream == b"data: caf\xc3\xa9\n\n"


# ----- retry pacing (RETRY-19) --------------------------------------------


@pytest.mark.req("RETRY-19")
def test_retry_pacing_classification_is_self_consistent() -> None:
    for vector in load_retry_pacing():
        token = vector.raw.decode("ascii")
        assert _float_ok(token) == vector.float_accepts, vector.id
        prescreen = bool(_PRESCREEN.match(token))
        if vector.kind == "reject":
            assert not prescreen and _parsedate_or_none(token) is None, vector.id
            assert vector.seconds is None, vector.id
        elif vector.kind == "delta":
            assert prescreen and vector.seconds == float(token), vector.id
            if vector.clamp_max is not None:
                assert vector.clamped == min(float(token), vector.clamp_max), vector.id
        elif vector.kind == "http_date":
            assert not prescreen, vector.id
            when = _parsedate_or_none(token)
            assert when is not None and vector.now is not None, vector.id
            delta = max(0.0, when.timestamp() - vector.now)
            assert vector.seconds == delta, vector.id
            if vector.clamp_max is not None:
                assert vector.clamped == min(delta, vector.clamp_max), vector.id
        else:
            pytest.fail(f"unknown kind {vector.kind!r} in {vector.id}")


def test_retry_pacing_prescreen_rejects_what_float_over_accepts() -> None:
    by_id = {vector.id: vector for vector in load_retry_pacing()}
    for vid in ("reject-nan-token", "reject-inf-token", "reject-underscore-grouping"):
        vector = by_id[vid]
        assert vector.float_accepts is True  # float() would take it...
        assert vector.kind == "reject" and vector.seconds is None  # ...pre-screen won't


def test_retry_pacing_past_date_zero_is_distinct_from_malformed_none() -> None:
    by_id = {vector.id: vector for vector in load_retry_pacing()}
    assert by_id["http-date-past-floors-to-zero"].seconds == 0.0
    assert by_id["reject-empty"].seconds is None


def test_retry_pacing_365_day_clamp() -> None:
    for vector in load_retry_pacing():
        if vector.clamp_max is not None:
            assert vector.clamp_max == 365 * 24 * 3600.0
            assert vector.clamped == vector.clamp_max, vector.id


# ----- RFC 1123 dates (CFG-29) --------------------------------------------


@pytest.mark.req("CFG-29")
def test_rfc1123_render_and_parse_match_oracle() -> None:
    for vector in load_rfc1123_dates():
        text = vector.text.decode("ascii")
        if vector.direction == "render":
            assert formatdate(vector.epoch, usegmt=True) == text, vector.id
        else:
            assert int(parsedate_to_datetime(text).timestamp()) == vector.epoch, vector.id
        assert vector.round_trippable == (formatdate(vector.epoch, usegmt=True) == text)


def test_rfc1123_tolerant_parse_named_cases() -> None:
    by_id = {vector.id: vector for vector in load_rfc1123_dates()}
    canonical = by_id["parse-canonical"].epoch
    # Informational weekday and zone aliases all resolve to the same instant.
    for vid in (
        "parse-informational-weekday",
        "parse-zone-alias-UT",
        "parse-zone-numeric-utc",
        "parse-zone-offset-plus0100",
    ):
        assert by_id[vid].epoch == canonical, vid
    assert by_id["parse-informational-weekday"].round_trippable is False
    assert by_id["parse-canonical"].round_trippable is True


# ----- furl-upgrade canary lane (non-blocking, opt-in) --------------------


@pytest.mark.skipif(
    os.environ.get("GOLDEN_CORPUS_FURL_CANARY") != "1",
    reason=(
        "furl-upgrade canary lane (opt-in): set GOLDEN_CORPUS_FURL_CANARY=1. "
        "TODO: wire into a latest-furl CI matrix so a furl upgrade that shifts "
        "URL byte-exactness is caught by re-running this corpus."
    ),
)
def test_furl_upgrade_canary_reloads_corpus() -> None:
    # Non-blocking stub: a future latest-furl lane reloads the whole corpus so
    # a furl upgrade cannot silently change URL byte-exactness unnoticed.
    corpus = load_all()
    assert set(corpus) == set(FAMILIES)
