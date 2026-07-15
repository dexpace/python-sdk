# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the shared pure resilience modules.

Covers the single-sourced formulas that the sync and async retry policies (and
error construction) import instead of restating: the retryability classifier
(status table + cause-chain walk), the backoff calculator (deterministic curve
+ jitter), and the pacing-header parsers.
"""

from __future__ import annotations

import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.pipeline._pure.backoff import (
    DEFAULT_BASE_DELAY,
    DEFAULT_MAX_DELAY,
    DEFAULT_MULTIPLIER,
    apply_jitter,
    compute_backoff,
)
from dexpace.sdk.core.pipeline._pure.classifier import (
    DEFAULT_RETRYABLE_STATUS,
    Retryable,
    default_status_is_retryable,
    is_retryable,
    status_is_retryable,
)
from dexpace.sdk.core.pipeline._pure.pacing import (
    MAX_RETRY_AFTER_SECONDS,
    parse_rate_limit_reset,
    parse_retry_after,
    parse_retry_after_ms,
)
from dexpace.sdk.tck.golden_corpus import RetryPacingVector, load_retry_pacing

# ----- classifier: status table -------------------------------------------


class TestStatusIsRetryable:
    @pytest.mark.parametrize("status", sorted(DEFAULT_RETRYABLE_STATUS))
    def test_default_set_members_are_retryable(self, status: int) -> None:
        assert status_is_retryable(status, DEFAULT_RETRYABLE_STATUS) is True

    def test_default_set_is_the_curated_transient_codes(self) -> None:
        # 408 (timeout), 429 (rate limit), and the transient 5xx family; the
        # permanent 501/505 are deliberately excluded.
        assert frozenset({408, 429, 500, 502, 503, 504}) == DEFAULT_RETRYABLE_STATUS

    @pytest.mark.parametrize("status", [200, 301, 400, 404, 409, 501, 505])
    def test_non_default_codes_are_not_retryable(self, status: int) -> None:
        assert status_is_retryable(status, DEFAULT_RETRYABLE_STATUS) is False

    def test_none_status_is_never_retryable(self) -> None:
        assert status_is_retryable(None, DEFAULT_RETRYABLE_STATUS) is False

    @pytest.mark.req("RECOV-17")
    def test_configurable_set_is_the_authority(self) -> None:
        # The passed-in set is consulted, not a hard-coded literal: a bespoke
        # set flips both directions relative to the default.
        custom = frozenset({418})
        assert status_is_retryable(418, custom) is True
        assert status_is_retryable(503, custom) is False

    def test_empty_set_disables_retry(self) -> None:
        assert status_is_retryable(503, frozenset()) is False


# ----- classifier: canonical baked-flag rule (408, 429, all 5xx but 501/505) -


class TestDefaultStatusIsRetryable:
    """The single canonical classifier behind a protocol error's baked flag.

    Distinct from ``status_is_retryable`` (the configurable policy gate): this
    is the fixed rule ``408, 429, all 5xx EXCEPT 501/505`` (XCUT-5), broader
    than the curated ``DEFAULT_RETRYABLE_STATUS`` policy default.
    """

    @pytest.mark.req("XCUT-5")
    @pytest.mark.parametrize("status", [408, 429])
    def test_two_retryable_client_codes(self, status: int) -> None:
        assert default_status_is_retryable(status) is True

    @pytest.mark.req("XCUT-5")
    @pytest.mark.parametrize("status", list(range(500, 600)))
    def test_every_5xx_except_501_and_505_is_retryable(self, status: int) -> None:
        expected = status not in (501, 505)
        assert default_status_is_retryable(status) is expected

    @pytest.mark.req("XCUT-5")
    def test_exotic_5xx_are_retryable_unlike_the_narrow_policy_default(self) -> None:
        # The exotic 5xx codes the narrow policy default omits still bake True:
        # the canonical classifier is a strict superset of the policy default.
        for status in (506, 507, 508, 510, 511):
            assert default_status_is_retryable(status) is True
            assert status not in DEFAULT_RETRYABLE_STATUS

    @pytest.mark.req("XCUT-5")
    @pytest.mark.parametrize("status", [200, 301, 400, 404, 409, 428, 499, 600])
    def test_non_retryable_codes(self, status: int) -> None:
        assert default_status_is_retryable(status) is False

    def test_none_is_never_retryable(self) -> None:
        assert default_status_is_retryable(None) is False

    def test_canonical_rule_is_a_superset_of_the_policy_default(self) -> None:
        # Every code the narrow policy default retries is also retryable under
        # the canonical rule; the reverse does not hold (the exotic 5xx above).
        for status in DEFAULT_RETRYABLE_STATUS:
            assert default_status_is_retryable(status) is True


# ----- classifier: cause-chain walk ----------------------------------------


class _FlaggedError(Exception):
    """Exception carrying an explicit ``retryable`` flag (satisfies Retryable)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TestRetryableProtocol:
    def test_object_with_flag_satisfies_protocol(self) -> None:
        assert isinstance(_FlaggedError("x", retryable=True), Retryable)

    def test_plain_exception_does_not_satisfy_protocol(self) -> None:
        assert not isinstance(ValueError("x"), Retryable)


class TestIsRetryable:
    def test_direct_retryable_true(self) -> None:
        assert is_retryable(_FlaggedError("boom", retryable=True)) is True

    def test_direct_retryable_false(self) -> None:
        assert is_retryable(_FlaggedError("boom", retryable=False)) is False

    def test_plain_exception_is_not_retryable(self) -> None:
        assert is_retryable(ValueError("boom")) is False

    def test_retryable_via_explicit_cause(self) -> None:
        outer = ValueError("outer")
        outer.__cause__ = _FlaggedError("inner", retryable=True)
        assert is_retryable(outer) is True

    def test_retryable_via_implicit_context(self) -> None:
        outer = ValueError("outer")
        outer.__context__ = _FlaggedError("inner", retryable=True)
        assert is_retryable(outer) is True

    @pytest.mark.req("RETRY-2")
    def test_retryable_deep_in_chain(self) -> None:
        deepest = _FlaggedError("deepest", retryable=True)
        middle = ValueError("middle")
        middle.__cause__ = deepest
        top = ValueError("top")
        top.__cause__ = middle
        assert is_retryable(top) is True

    @pytest.mark.req("RETRY-2", "XCUT-9")
    def test_self_referential_chain_terminates(self) -> None:
        exc = ValueError("loop")
        exc.__cause__ = exc
        assert is_retryable(exc) is False

    @pytest.mark.req("RETRY-2", "XCUT-9")
    def test_two_node_cycle_terminates(self) -> None:
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        assert is_retryable(first) is False

    @pytest.mark.req("XCUT-9")
    def test_cycle_with_retryable_node_returns_true(self) -> None:
        retryable = _FlaggedError("retryable", retryable=True)
        other = ValueError("other")
        other.__cause__ = retryable
        retryable.__cause__ = other  # cycle, but a retryable node is reachable
        assert is_retryable(other) is True


# ----- backoff: deterministic curve ----------------------------------------


class TestComputeBackoff:
    def test_first_attempt_is_zero(self) -> None:
        assert compute_backoff(0, base=1.0, cap=100.0) == 0.0
        assert compute_backoff(1, base=1.0, cap=100.0) == 0.0

    def test_exponential_growth(self) -> None:
        assert compute_backoff(2, base=1.0, cap=1000.0, multiplier=2.0) == 2.0
        assert compute_backoff(3, base=1.0, cap=1000.0, multiplier=2.0) == 4.0
        assert compute_backoff(4, base=1.0, cap=1000.0, multiplier=2.0) == 8.0

    @pytest.mark.req("RETRY-11")
    def test_cap_saturates(self) -> None:
        assert compute_backoff(20, base=1.0, cap=8.0, multiplier=2.0) == 8.0

    @pytest.mark.req("RETRY-43")
    def test_fixed_mode_is_constant(self) -> None:
        assert compute_backoff(1, base=0.5, cap=100.0, fixed=True) == 0.0
        assert compute_backoff(2, base=0.5, cap=100.0, fixed=True) == 0.5
        assert compute_backoff(9, base=0.5, cap=100.0, fixed=True) == 0.5

    def test_module_defaults_match_spec(self) -> None:
        # 0.2s initial, x2 multiplier, 8s cap.
        assert (DEFAULT_BASE_DELAY, DEFAULT_MULTIPLIER, DEFAULT_MAX_DELAY) == (0.2, 2.0, 8.0)
        assert compute_backoff(2) == pytest.approx(0.4)
        assert compute_backoff(3) == pytest.approx(0.8)

    def test_hand_enumerated_schedule_is_non_decreasing(self) -> None:
        delays = [compute_backoff(n, base=0.2, cap=8.0, multiplier=2.0) for n in range(10)]
        assert delays == sorted(delays)
        assert delays[0] == 0.0
        assert delays[-1] == 8.0  # saturated at the cap

    @pytest.mark.req("RETRY-11")
    @given(
        attempt=st.integers(min_value=1, max_value=40),
        base=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        cap=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        multiplier=st.floats(min_value=1.0, max_value=5.0, allow_nan=False, allow_infinity=False),
    )
    def test_monotonic_pre_jitter(
        self, attempt: int, base: float, cap: float, multiplier: float
    ) -> None:
        # The pre-jitter schedule never decreases as the attempt count rises.
        current = compute_backoff(attempt, base=base, cap=cap, multiplier=multiplier)
        following = compute_backoff(attempt + 1, base=base, cap=cap, multiplier=multiplier)
        assert current <= following


# ----- backoff: jitter -----------------------------------------------------


class TestApplyJitter:
    def test_zero_delay_returns_zero_without_drawing(self) -> None:
        class _ExplodingRandom(random.Random):
            def uniform(self, a: float, b: float) -> float:  # pragma: no cover - must not run
                raise AssertionError("jitter must not draw for a zero delay")

        assert apply_jitter(0.0, _ExplodingRandom(), full_jitter=True) == 0.0

    @pytest.mark.req("RETRY-31")
    def test_full_jitter_stays_within_band(self) -> None:
        rng = random.Random(7)
        for _ in range(200):
            result = apply_jitter(10.0, rng, full_jitter=True)
            assert 5.0 <= result <= 10.0

    @pytest.mark.req("RETRY-10")
    def test_symmetric_zero_returns_delay_unchanged(self) -> None:
        assert apply_jitter(4.0, random.Random(1), full_jitter=False, symmetric=0.0) == 4.0

    @pytest.mark.req("RETRY-10")
    def test_symmetric_band(self) -> None:
        rng = random.Random(3)
        for _ in range(200):
            result = apply_jitter(8.0, rng, full_jitter=False, symmetric=0.25)
            assert 6.0 <= result <= 10.0

    def test_seeded_jitter_is_reproducible(self) -> None:
        first = [apply_jitter(5.0, random.Random(99), full_jitter=True) for _ in range(1)]
        second = [apply_jitter(5.0, random.Random(99), full_jitter=True) for _ in range(1)]
        assert first == second


# ----- pacing --------------------------------------------------------------

#: 365-day sanity ceiling, restated as a literal so a drift in the production
#: constant is caught here rather than tautologically re-derived from it.
_365_DAYS_SECONDS = 365 * 24 * 60 * 60


class TestParseRetryAfter:
    def test_delta_seconds(self) -> None:
        assert parse_retry_after("5", now=0.0) == pytest.approx(5.0)
        assert parse_retry_after("0", now=0.0) == pytest.approx(0.0)
        assert parse_retry_after("0.5", now=0.0) == pytest.approx(0.5)

    def test_surrounding_ascii_whitespace_is_tolerated(self) -> None:
        # RFC 7231 OWS around the numeric core is stripped; the core stays strict.
        assert parse_retry_after(" 30 ", now=0.0) == pytest.approx(30.0)
        assert parse_retry_after("\t30\t", now=0.0) == pytest.approx(30.0)

    def test_http_date_uses_injected_now(self) -> None:
        epoch_2000 = 946_684_800.0
        assert parse_retry_after("Sat, 01 Jan 2000 00:00:00 GMT", now=epoch_2000 - 60.0) == (
            pytest.approx(60.0)
        )

    @pytest.mark.req("CFG-30")
    def test_http_date_tolerates_zone_aliases_and_informational_weekday(self) -> None:
        # The date grammar is shared with util.http_date: tolerant on the
        # weekday name / zone alias, strict on the numeric structure.
        epoch = 946_684_800.0  # Sat, 01 Jan 2000 00:00:00 UTC
        assert parse_retry_after("Sat, 01 Jan 2000 00:00:00 UTC", now=epoch - 10.0) == (
            pytest.approx(10.0)
        )
        # A wrong-but-informational weekday name is ignored (only the date counts).
        assert parse_retry_after("Mon, 01 Jan 2000 00:00:00 GMT", now=epoch - 10.0) == (
            pytest.approx(10.0)
        )

    @pytest.mark.req("RECOV-23", "RETRY-17")
    def test_past_http_date_floors_to_zero_not_none(self) -> None:
        # NOTE (past-date -> 0 vs malformed -> None): a date already in the past
        # is a *valid* signal meaning "retry now" and floors to 0.0; only an
        # unparseable value is the absence-of-signal None.
        assert parse_retry_after("Mon, 01 Jan 1990 00:00:00 GMT", now=2_000_000_000.0) == 0.0
        assert parse_retry_after("not-a-date", now=2_000_000_000.0) is None

    @pytest.mark.req("RECOV-23", "RETRY-16")
    def test_invalid_and_missing_return_none(self) -> None:
        assert parse_retry_after("not-a-date", now=0.0) is None
        assert parse_retry_after("", now=0.0) is None
        assert parse_retry_after("   ", now=0.0) is None
        assert parse_retry_after(None, now=0.0) is None

    @pytest.mark.req("RETRY-19")
    @pytest.mark.parametrize(
        "token",
        # float() would accept every one of these; the strict pre-screen must not.
        # The last two are non-ASCII digits (Arabic-Indic and fullwidth "3")
        # that float() happily reads as 3 but the ASCII-only grammar rejects.
        ["nan", "inf", "+30", "-30", "1_000", "1e3", "0x1p4", "\u0663", "\uff13"],
    )
    def test_strict_screen_rejects_what_float_over_accepts(self, token: str) -> None:
        assert parse_retry_after(token, now=0.0) is None

    def test_multi_dot_and_trailing_unit_rejected(self) -> None:
        assert parse_retry_after("1.2.3", now=0.0) is None
        assert parse_retry_after("30d", now=0.0) is None

    @pytest.mark.req("RECOV-26", "RETRY-18")
    def test_365_day_clamp_caps_absurd_delta(self) -> None:
        # NOTE (365-day clamp): a hint far in the future is capped, not obeyed.
        assert parse_retry_after("999999999", now=0.0) == pytest.approx(_365_DAYS_SECONDS)
        assert MAX_RETRY_AFTER_SECONDS == _365_DAYS_SECONDS

    def test_max_delay_override_disables_and_tightens_clamp(self) -> None:
        # math.inf yields the raw un-clamped delta; a tighter ceiling caps sooner.
        assert parse_retry_after("999999999", now=0.0, max_delay=math.inf) == pytest.approx(
            999_999_999.0
        )
        assert parse_retry_after("999999999", now=0.0, max_delay=10.0) == pytest.approx(10.0)


class TestParseRetryAfterMs:
    """The millisecond-delta parser shared by retry-after-ms / x-ms-retry-after-ms."""

    @pytest.mark.req("RETRY-15", "RECOV-24")
    def test_whole_milliseconds_convert_to_seconds(self) -> None:
        assert parse_retry_after_ms("1500") == pytest.approx(1.5)
        assert parse_retry_after_ms("0") == pytest.approx(0.0)
        assert parse_retry_after_ms("250") == pytest.approx(0.25)

    def test_surrounding_ascii_whitespace_is_tolerated(self) -> None:
        assert parse_retry_after_ms(" 2000 ") == pytest.approx(2.0)
        assert parse_retry_after_ms("\t2000\t") == pytest.approx(2.0)

    @pytest.mark.req("RETRY-15", "RECOV-24")
    def test_missing_blank_or_non_integer_returns_none(self) -> None:
        assert parse_retry_after_ms(None) is None
        assert parse_retry_after_ms("") is None
        assert parse_retry_after_ms("   ") is None
        assert parse_retry_after_ms("soon") is None

    @pytest.mark.req("RECOV-24")
    @pytest.mark.parametrize(
        "token",
        # Integer grammar: fractional and every float()-permissive form reject.
        # The last two are non-ASCII digits (Arabic-Indic and fullwidth "3").
        ["1.5", "nan", "inf", "+30", "-30", "1_000", "1e3", "0x1p4", "\u0663", "\uff13"],
    )
    def test_strict_integer_screen_rejects_non_integers(self, token: str) -> None:
        assert parse_retry_after_ms(token) is None

    def test_365_day_clamp_caps_absurd_delay(self) -> None:
        assert parse_retry_after_ms(str(10**15)) == pytest.approx(_365_DAYS_SECONDS)
        assert parse_retry_after_ms(str(10**15), max_delay=math.inf) == pytest.approx(10**12)


class TestParseRateLimitReset:
    def test_future_epoch_positive_delay(self) -> None:
        assert parse_rate_limit_reset("150", now=100.0) == 50.0

    @pytest.mark.req("RETRY-17")
    def test_past_epoch_floors_at_zero(self) -> None:
        assert parse_rate_limit_reset("90", now=100.0) == 0.0

    def test_missing_blank_or_non_numeric_returns_none(self) -> None:
        assert parse_rate_limit_reset(None, now=0.0) is None
        assert parse_rate_limit_reset("   ", now=0.0) is None
        assert parse_rate_limit_reset("soon", now=0.0) is None

    def test_strict_screen_rejects_float_over_accepted_epochs(self) -> None:
        for token in ("nan", "inf", "1_000", "+150", "1e3"):
            assert parse_rate_limit_reset(token, now=0.0) is None

    @pytest.mark.req("RETRY-18")
    def test_365_day_clamp_caps_absurd_reset(self) -> None:
        # A reset epoch absurdly far in the future is capped at the 365-day ceiling.
        assert parse_rate_limit_reset("999999999999", now=0.0) == pytest.approx(_365_DAYS_SECONDS)


class TestPacingPrecedence:
    @pytest.mark.req("RETRY-21")
    def test_retry_after_is_authoritative_over_rate_limit_reset(self) -> None:
        # NOTE (precedence order): the resolution order is Retry-After (seconds
        # or HTTP-date), then X-RateLimit-Reset. Enforced in
        # RetryPolicy._server_delay and proven end-to-end in
        # test_retry_tuning.TestRateLimitResetHonored; here we pin the pure
        # building blocks the policy composes in that order.
        now = 1_000.0
        retry_after = parse_retry_after("5", now)
        reset = parse_rate_limit_reset("9999999", now)
        assert retry_after is not None and reset is not None
        # A present Retry-After wins; the reset value is only the fallback.
        resolved = retry_after if retry_after is not None else reset
        assert resolved == pytest.approx(5.0)
        # With Retry-After absent/unparseable, the reset fallback is consulted.
        absent = parse_retry_after("garbage", now)
        assert (absent if absent is not None else reset) == reset


class TestPacingGoldenCorpus:
    """Point the production parser at every ``retry_pacing`` corpus vector.

    The corpus (RETRY-19 family) is the byte-exact source of truth for the
    pacing grammar; ``test_golden_corpus`` validates the fixtures with stdlib
    oracles, while this suite proves the shipped ``parse_retry_after`` agrees
    with every one of them — including that no reject vector ever raises.
    """

    @pytest.mark.req("RETRY-19")
    @pytest.mark.parametrize("vector", load_retry_pacing(), ids=lambda v: v.id)
    def test_vector_matches_production_parser(self, vector: RetryPacingVector) -> None:
        token = vector.raw.decode("ascii")
        now = vector.now if vector.now is not None else 0.0
        # max_delay=inf recovers the pre-clamp seconds the vector pins; the call
        # must never raise, for any vector (rejects included).
        raw_parsed = parse_retry_after(token, now, max_delay=math.inf)
        if vector.kind == "reject":
            assert raw_parsed is None, vector.id
            assert parse_retry_after(token, now) is None, vector.id
            return
        assert raw_parsed == pytest.approx(vector.seconds), vector.id
        if vector.clamp_max is not None:
            clamped = parse_retry_after(token, now, max_delay=vector.clamp_max)
            assert clamped == pytest.approx(vector.clamped), vector.id
            # The default 365-day ceiling reproduces the pinned clamp result.
            assert parse_retry_after(token, now) == pytest.approx(vector.clamped), vector.id
        else:
            # No clamp expected: the default ceiling leaves the value untouched.
            assert parse_retry_after(token, now) == pytest.approx(vector.seconds), vector.id

    def test_reject_vectors_diverge_from_float(self) -> None:
        # Directly pins the "screened before float()" property against the
        # corpus's own float_accepts flag: every reject that float would take.
        for vector in load_retry_pacing():
            if vector.kind != "reject" or not vector.float_accepts:
                continue
            token = vector.raw.decode("ascii")
            assert isinstance(float(token), float)  # float() accepts it (no raise)...
            assert parse_retry_after(token, now=0.0) is None, vector.id  # ...the parser won't


class TestPacingTotality:
    """The parsers are total: they return None or a sane delay, never raise."""

    @pytest.mark.req("RECOV-23", "RECOV-26", "RECOV-29", "RETRY-16", "RETRY-22")
    @given(
        value=st.one_of(
            st.none(),
            st.text(),
            st.binary().map(lambda b: b.decode("latin-1")),
        ),
        now=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12),
    )
    def test_never_raises_for_arbitrary_input(self, value: str | None, now: float) -> None:
        for result in (parse_retry_after(value, now), parse_rate_limit_reset(value, now)):
            assert result is None or (
                isinstance(result, float)
                and math.isfinite(result)
                and 0.0 <= result <= MAX_RETRY_AFTER_SECONDS
            )
