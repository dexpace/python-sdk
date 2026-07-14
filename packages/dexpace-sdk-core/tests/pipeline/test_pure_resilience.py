# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the shared pure resilience modules.

Covers the single-sourced formulas that the sync and async retry policies (and
error construction) import instead of restating: the retryability classifier
(status table + cause-chain walk), the backoff calculator (deterministic curve
+ jitter), and the pacing-header parsers.
"""

from __future__ import annotations

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
    is_retryable,
    status_is_retryable,
)
from dexpace.sdk.core.pipeline._pure.pacing import (
    parse_rate_limit_reset,
    parse_retry_after,
)

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

    def test_configurable_set_is_the_authority(self) -> None:
        # The passed-in set is consulted, not a hard-coded literal: a bespoke
        # set flips both directions relative to the default.
        custom = frozenset({418})
        assert status_is_retryable(418, custom) is True
        assert status_is_retryable(503, custom) is False

    def test_empty_set_disables_retry(self) -> None:
        assert status_is_retryable(503, frozenset()) is False


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

    def test_retryable_deep_in_chain(self) -> None:
        deepest = _FlaggedError("deepest", retryable=True)
        middle = ValueError("middle")
        middle.__cause__ = deepest
        top = ValueError("top")
        top.__cause__ = middle
        assert is_retryable(top) is True

    def test_self_referential_chain_terminates(self) -> None:
        exc = ValueError("loop")
        exc.__cause__ = exc
        assert is_retryable(exc) is False

    def test_two_node_cycle_terminates(self) -> None:
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        assert is_retryable(first) is False

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

    def test_cap_saturates(self) -> None:
        assert compute_backoff(20, base=1.0, cap=8.0, multiplier=2.0) == 8.0

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

    def test_full_jitter_stays_within_band(self) -> None:
        rng = random.Random(7)
        for _ in range(200):
            result = apply_jitter(10.0, rng, full_jitter=True)
            assert 5.0 <= result <= 10.0

    def test_symmetric_zero_returns_delay_unchanged(self) -> None:
        assert apply_jitter(4.0, random.Random(1), full_jitter=False, symmetric=0.0) == 4.0

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


class TestParseRetryAfter:
    def test_delta_seconds(self) -> None:
        assert parse_retry_after("5", now=0.0) == pytest.approx(5.0)
        assert parse_retry_after("0", now=0.0) == pytest.approx(0.0)
        assert parse_retry_after("0.5", now=0.0) == pytest.approx(0.5)

    def test_http_date_uses_injected_now(self) -> None:
        epoch_2000 = 946_684_800.0
        assert parse_retry_after("Sat, 01 Jan 2000 00:00:00 GMT", now=epoch_2000 - 60.0) == (
            pytest.approx(60.0)
        )

    def test_http_date_past_clamps_to_zero(self) -> None:
        assert parse_retry_after("Mon, 01 Jan 1990 00:00:00 GMT", now=2_000_000_000.0) == (
            pytest.approx(0.0)
        )

    def test_invalid_and_missing_return_none(self) -> None:
        assert parse_retry_after("not-a-date", now=0.0) is None
        assert parse_retry_after("", now=0.0) is None
        assert parse_retry_after(None, now=0.0) is None


class TestParseRateLimitReset:
    def test_future_epoch_positive_delay(self) -> None:
        assert parse_rate_limit_reset("150", now=100.0) == 50.0

    def test_past_epoch_floors_at_zero(self) -> None:
        assert parse_rate_limit_reset("90", now=100.0) == 0.0

    def test_missing_blank_or_non_numeric_returns_none(self) -> None:
        assert parse_rate_limit_reset(None, now=0.0) is None
        assert parse_rate_limit_reset("   ", now=0.0) is None
        assert parse_rate_limit_reset("soon", now=0.0) is None
