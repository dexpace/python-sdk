# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``Configuration`` — layered override + env + property lookup."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

from dexpace.sdk.core.config import (
    Configuration,
    ConfigurationBuilder,
    default_configuration,
    set_default_configuration,
)
from dexpace.sdk.core.config import configuration as _configuration


def _env(mapping: dict[str, str]) -> Callable[[str], str | None]:
    """Build a fake env source closing over ``mapping``."""

    def lookup(name: str) -> str | None:
        return mapping.get(name)

    return lookup


def _props(mapping: dict[str, str]) -> Callable[[str], str | None]:
    """Build a fake property source closing over ``mapping``."""

    def lookup(name: str) -> str | None:
        return mapping.get(name)

    return lookup


# ---------------------------------------------------------------------------
# Lookup ordering


def test_get_returns_override_over_env_over_default() -> None:
    cfg = Configuration(
        overrides={"KEY": "override-value"},
        env=_env({"KEY": "env-value"}),
    )
    assert cfg.get("KEY", "default") == "override-value"


def test_get_returns_env_when_no_override() -> None:
    cfg = Configuration(env=_env({"KEY": "env-value"}))
    assert cfg.get("KEY", "default") == "env-value"


def test_get_returns_default_when_neither_override_nor_env() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get("KEY", "default") == "default"


def test_get_returns_none_when_default_is_none_and_missing() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get("KEY") is None


@pytest.mark.req("CFG-2")
def test_empty_env_value_treated_as_absent() -> None:
    cfg = Configuration(env=_env({"KEY": ""}))
    assert cfg.get("KEY", "default") == "default"


def test_empty_override_value_is_respected() -> None:
    # Explicit empty override is intentional and wins.
    cfg = Configuration(overrides={"KEY": ""}, env=_env({"KEY": "env"}))
    assert cfg.get("KEY", "default") == ""


# ---------------------------------------------------------------------------
# get_int


def test_get_int_happy_path() -> None:
    cfg = Configuration(env=_env({"N": "42"}))
    assert cfg.get_int("N", 7) == 42


@pytest.mark.req("CFG-5")
def test_get_int_parse_failure_returns_default() -> None:
    cfg = Configuration(env=_env({"N": "not-a-number"}))
    assert cfg.get_int("N", 7) == 7


def test_get_int_missing_returns_default() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get_int("N", 7) == 7


def test_get_int_override_beats_env() -> None:
    cfg = Configuration(overrides={"N": "100"}, env=_env({"N": "50"}))
    assert cfg.get_int("N", 0) == 100


# ---------------------------------------------------------------------------
# get_bool — strict


@pytest.mark.req("CFG-6")
@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("false", False),
        ("FALSE", False),
        ("False", False),
    ],
)
def test_get_bool_recognized_values(value: str, expected: bool) -> None:
    cfg = Configuration(env=_env({"FLAG": value}))
    assert cfg.get_bool("FLAG", not expected) is expected


@pytest.mark.req("CFG-6")
@pytest.mark.parametrize("value", ["1", "0", "yes", "no", "on", "off", "", "garbage"])
def test_get_bool_strict_rejects_non_boolean_literals(value: str) -> None:
    cfg = Configuration(env=_env({"FLAG": value} if value else {}))
    # Default should be returned for every non-true/false token.
    assert cfg.get_bool("FLAG", True) is True
    assert cfg.get_bool("FLAG", False) is False


def test_get_bool_missing_returns_default() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get_bool("FLAG", True) is True


# ---------------------------------------------------------------------------
# get_duration


@pytest.mark.parametrize(
    "raw,expected_seconds",
    [
        ("PT5S", 5.0),
        ("PT1M", 60.0),
        ("PT2H", 7200.0),
        ("P1D", 86400.0),
        ("PT0.5S", 0.5),
        ("500ms", 0.5),
        ("5s", 5.0),
        ("1m", 60.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("1.5", 1.5),
        ("30", 30.0),
    ],
)
def test_get_duration_parses_supported_formats(raw: str, expected_seconds: float) -> None:
    cfg = Configuration(env=_env({"T": raw}))
    assert cfg.get_duration("T", 0.0) == pytest.approx(expected_seconds)


@pytest.mark.req("CFG-5")
def test_get_duration_parse_failure_returns_default() -> None:
    cfg = Configuration(env=_env({"T": "nonsense"}))
    assert cfg.get_duration("T", 9.0) == 9.0


def test_get_duration_missing_returns_default() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get_duration("T", 9.0) == 9.0


def test_get_duration_negative_returns_default() -> None:
    cfg = Configuration(env=_env({"T": "-5s"}))
    assert cfg.get_duration("T", 9.0) == 9.0


@pytest.mark.parametrize("value", ["inf", "-inf", "Infinity", "nan", "NaN"])
def test_get_duration_non_finite_returns_default(value: str) -> None:
    # ``float('inf')`` / ``float('nan')`` parse successfully but a non-finite
    # duration disables deadline enforcement; reject them in favour of the default.
    cfg = Configuration(env=_env({"T": value}))
    assert cfg.get_duration("T", 9.0) == 9.0


# ---------------------------------------------------------------------------
# Builder


def test_builder_fluent_chaining() -> None:
    cfg = Configuration.builder().put("A", "1").put("B", "two").put("C", "true").build()
    assert cfg.get("A", None) == "1"
    assert cfg.get("B", None) == "two"
    assert cfg.get_bool("C", False) is True


def test_builder_with_env_source() -> None:
    cfg = ConfigurationBuilder().put("A", "1").env(_env({"B": "from-env"})).build()
    assert cfg.get("A", None) == "1"
    assert cfg.get("B", None) == "from-env"


def test_builder_put_overrides_previous_value() -> None:
    cfg = Configuration.builder().put("KEY", "first").put("KEY", "second").build()
    assert cfg.get("KEY", None) == "second"


# ---------------------------------------------------------------------------
# Well-known keys


def test_well_known_constants_exposed() -> None:
    assert Configuration.MAX_RETRY_ATTEMPTS == "MAX_RETRY_ATTEMPTS"
    assert Configuration.REQUEST_RETRY_DEFAULT_TIMEOUT == "REQUEST_RETRY_DEFAULT_TIMEOUT"
    assert Configuration.HTTPS_PROXY == "HTTPS_PROXY"
    assert Configuration.HTTP_PROXY == "HTTP_PROXY"
    assert Configuration.NO_PROXY == "NO_PROXY"


@pytest.mark.req("CFG-11")
def test_default_env_is_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPACE_TEST_CONFIG_KEY", "value-from-os")
    cfg = Configuration()
    assert cfg.get("DEXPACE_TEST_CONFIG_KEY", None) == "value-from-os"


class TestOverrideImmutability:
    @pytest.mark.req("CFG-8")
    def test_external_mutation_of_overrides_does_not_leak(self) -> None:
        """A defensive copy means callers can't mutate config after construction."""
        original = {"KEY": "initial"}
        cfg = Configuration(overrides=original)
        original["KEY"] = "mutated-after-construction"
        assert cfg.get("KEY") == "initial"

    @pytest.mark.req("CFG-12", "CFG-8")
    def test_builder_build_takes_defensive_copy(self) -> None:
        builder = Configuration.builder().put("KEY", "v1")
        cfg = builder.build()
        builder.put("KEY", "v2")
        # The originally-built config should not see subsequent builder edits.
        assert cfg.get("KEY") == "v1"


# ---------------------------------------------------------------------------
# Precedence matrix: override > env > property-seam > default
#
# The property seam is keyed by a dotted-lowercase normalisation of the name,
# so the env-style ``FOO_BAR`` resolves against the property key ``foo.bar``.


_MATRIX_KEY = "FOO_BAR"
_MATRIX_PROP_KEY = "foo.bar"


@pytest.mark.req("CFG-1", "CFG-2")
@pytest.mark.parametrize(
    "overrides,env,props,expected",
    [
        # All four layers populated -> override wins.
        ({_MATRIX_KEY: "ov"}, {_MATRIX_KEY: "en"}, {_MATRIX_PROP_KEY: "pr"}, "ov"),
        # No override -> env wins over property and default.
        ({}, {_MATRIX_KEY: "en"}, {_MATRIX_PROP_KEY: "pr"}, "en"),
        # No override, no env -> property (normalised key) wins over default.
        ({}, {}, {_MATRIX_PROP_KEY: "pr"}, "pr"),
        # Nothing populated -> default.
        ({}, {}, {}, "def"),
        # Empty env value is absent -> falls through to property.
        ({}, {_MATRIX_KEY: ""}, {_MATRIX_PROP_KEY: "pr"}, "pr"),
        # Empty property value is absent -> falls through to default.
        ({}, {}, {_MATRIX_PROP_KEY: ""}, "def"),
    ],
)
def test_precedence_matrix(
    overrides: dict[str, str],
    env: dict[str, str],
    props: dict[str, str],
    expected: str,
) -> None:
    cfg = Configuration(overrides=overrides, env=_env(env), props=_props(props))
    assert cfg.get(_MATRIX_KEY, "def") == expected


@pytest.mark.req("CFG-1", "CFG-3")
def test_property_seam_is_normalised_by_get() -> None:
    # ``get`` normalises the env-style key to the dotted-lowercase property key.
    cfg = Configuration(props=_props({_MATRIX_PROP_KEY: "value"}))
    assert cfg.get(_MATRIX_KEY, "def") == "value"


@pytest.mark.req("CFG-3")
def test_property_seam_not_found_with_unnormalised_key_via_get() -> None:
    # A property source keyed by the raw env-style name is NOT seen by ``get``,
    # which only queries the normalised key.
    cfg = Configuration(props=_props({_MATRIX_KEY: "value"}))
    assert cfg.get(_MATRIX_KEY, "def") == "def"


# ---------------------------------------------------------------------------
# Raw accessor: skips property-key normalisation


@pytest.mark.req("CFG-4")
def test_get_raw_queries_property_with_verbatim_key() -> None:
    cfg = Configuration(props=_props({_MATRIX_KEY: "raw-value"}))
    # ``get_raw`` looks the property source up under the verbatim name...
    assert cfg.get_raw(_MATRIX_KEY, "def") == "raw-value"
    # ...while ``get`` normalises and therefore misses it.
    assert cfg.get(_MATRIX_KEY, "def") == "def"


@pytest.mark.req("CFG-4")
def test_get_raw_still_honours_override_and_env() -> None:
    cfg = Configuration(
        overrides={_MATRIX_KEY: "ov"},
        env=_env({_MATRIX_KEY: "en"}),
        props=_props({_MATRIX_KEY: "raw"}),
    )
    assert cfg.get_raw(_MATRIX_KEY, "def") == "ov"


def test_get_raw_falls_back_to_env_over_property() -> None:
    cfg = Configuration(
        env=_env({_MATRIX_KEY: "en"}),
        props=_props({_MATRIX_KEY: "raw"}),
    )
    assert cfg.get_raw(_MATRIX_KEY, "def") == "en"


# ---------------------------------------------------------------------------
# Typed accessors resolve through the FULL layered lookup (incl. property seam)


@pytest.mark.req("CFG-38")
def test_get_int_resolves_via_property_seam() -> None:
    cfg = Configuration(props=_props({_MATRIX_PROP_KEY: "42"}))
    assert cfg.get_int(_MATRIX_KEY, 7) == 42


@pytest.mark.req("CFG-38")
def test_get_bool_resolves_via_property_seam() -> None:
    cfg = Configuration(props=_props({_MATRIX_PROP_KEY: "true"}))
    assert cfg.get_bool(_MATRIX_KEY, False) is True


@pytest.mark.req("CFG-38")
def test_get_duration_resolves_via_property_seam() -> None:
    cfg = Configuration(props=_props({_MATRIX_PROP_KEY: "5s"}))
    assert cfg.get_duration(_MATRIX_KEY, 0.0) == pytest.approx(5.0)


@pytest.mark.req("CFG-5")
def test_get_int_negative_is_valid() -> None:
    # Negative values are legitimate for plain int config (unlike durations).
    cfg = Configuration(env=_env({"N": "-3"}))
    assert cfg.get_int("N", 0) == -3


# ---------------------------------------------------------------------------
# Property seam via the builder


@pytest.mark.req("CFG-11")
def test_builder_with_property_source() -> None:
    cfg = ConfigurationBuilder().props(_props({_MATRIX_PROP_KEY: "pv"})).build()
    assert cfg.get(_MATRIX_KEY, "def") == "pv"


# ---------------------------------------------------------------------------
# Derivation (copy-on-write) shares seams


@pytest.mark.req("CFG-9")
def test_with_override_is_copy_on_write() -> None:
    base = Configuration(env=_env({"KEY": "env"}))
    derived = base.with_override("KEY", "derived")
    assert derived.get("KEY", "def") == "derived"
    # The source is untouched — still resolves via its env layer.
    assert base.get("KEY", "def") == "env"


@pytest.mark.req("CFG-9")
def test_with_override_shares_source_seams() -> None:
    env = _env({})
    props = _props({_MATRIX_PROP_KEY: "shared"})
    base = Configuration(env=env, props=props)
    derived = base.with_override("OTHER", "x")
    # Same seam objects — a single injected fake drives both configurations.
    assert derived.env is env
    assert derived.props is props
    assert derived.get(_MATRIX_KEY, "def") == "shared"


@pytest.mark.req("CFG-9")
def test_with_override_does_not_mutate_source_overrides() -> None:
    base = Configuration(overrides={"A": "1"})
    base.with_override("A", "2")
    assert base.get("A") == "1"


# ---------------------------------------------------------------------------
# None-rejection for required arguments


@pytest.mark.req("CFG-37")
def test_get_rejects_none_key() -> None:
    cfg = Configuration()
    with pytest.raises(TypeError):
        cfg.get(None)  # type: ignore[arg-type]


def test_get_raw_rejects_none_key() -> None:
    cfg = Configuration()
    with pytest.raises(TypeError):
        cfg.get_raw(None)  # type: ignore[arg-type]


@pytest.mark.req("CFG-37")
def test_with_override_rejects_none_key() -> None:
    cfg = Configuration()
    with pytest.raises(TypeError):
        cfg.with_override(None, "v")  # type: ignore[arg-type]


@pytest.mark.req("CFG-37")
def test_set_default_configuration_rejects_none() -> None:
    with pytest.raises(TypeError):
        set_default_configuration(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Global default slot (last-write-wins)


@pytest.fixture
def _restore_default_slot() -> Iterator[None]:
    """Snapshot and restore the module-global default slot around a test."""
    saved = _configuration._default_configuration
    try:
        yield
    finally:
        _configuration._default_configuration = saved


@pytest.mark.req("CFG-13")
def test_default_configuration_lazily_initialises(_restore_default_slot: None) -> None:
    _configuration._default_configuration = None
    cfg = default_configuration()
    assert isinstance(cfg, Configuration)
    # Repeat access returns the same lazily-created instance.
    assert default_configuration() is cfg


@pytest.mark.req("CFG-13")
def test_set_default_configuration_last_write_wins(_restore_default_slot: None) -> None:
    first = Configuration(overrides={"K": "first"})
    second = Configuration(overrides={"K": "second"})
    set_default_configuration(first)
    assert default_configuration() is first
    set_default_configuration(second)
    assert default_configuration() is second
    assert default_configuration().get("K") == "second"


# ---------------------------------------------------------------------------
# Hermetic seam substitution — tests never touch real ``os.environ``


@pytest.mark.req("CFG-11")
def test_injected_env_seam_bypasses_real_os_environ() -> None:
    sentinel = "DEXPACE_HERMETIC_SENTINEL_KEY"
    # Precondition: the sentinel is genuinely absent from the real process env.
    assert sentinel not in os.environ
    cfg = Configuration(env=_env({sentinel: "from-fake-seam"}))
    # The value is resolved purely from the injected seam...
    assert cfg.get(sentinel, "def") == "from-fake-seam"
    # ...and no test-visible mutation ever leaked into the real environment.
    assert sentinel not in os.environ


def test_real_os_environ_value_is_invisible_when_seam_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with a real env var present, an injected empty seam hides it —
    # proving the lookup consults the seam, not ``os.environ`` directly.
    monkeypatch.setenv("DEXPACE_HERMETIC_REAL_KEY", "real")
    cfg = Configuration(env=_env({}))
    assert cfg.get("DEXPACE_HERMETIC_REAL_KEY", "def") == "def"


# ---------------------------------------------------------------------------
# Duration-grammar corpus
#
# Note: this port interprets a *bare* number as SECONDS (not milliseconds),
# matching Python's ``time.sleep`` / ``Clock`` seconds convention. The
# shorthand ``500ms`` remains the way to express sub-second millisecond values.


@pytest.mark.parametrize(
    "raw,expected_seconds",
    [
        # ISO-8601 components (day / hour / minute / second, and combinations).
        ("PT5S", 5.0),
        ("PT1M", 60.0),
        ("PT2H", 7200.0),
        ("P1D", 86400.0),
        ("PT0.5S", 0.5),
        ("PT1M30S", 90.0),
        ("PT1H1M1S", 3661.0),
        ("P1DT2H", 93600.0),
        ("PT0S", 0.0),
        # Shorthand units (case-insensitive), including sub-second ms.
        ("500ms", 0.5),
        ("250MS", 0.25),
        ("5s", 5.0),
        ("1m", 60.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("1.5s", 1.5),
        ("  5s  ", 5.0),
        # Bare numbers are seconds.
        ("1.5", 1.5),
        ("30", 30.0),
        ("0", 0.0),
        ("  30  ", 30.0),
    ],
)
def test_duration_corpus_positive(raw: str, expected_seconds: float) -> None:
    cfg = Configuration(env=_env({"T": raw}))
    assert cfg.get_duration("T", -1.0) == pytest.approx(expected_seconds)


@pytest.mark.parametrize(
    "raw",
    [
        "nonsense",
        "",
        "   ",
        "5x",  # unknown unit
        "P",  # ISO prefix with no components
        "PT",  # ISO time marker with no components
        "P1Y",  # years unsupported
        "P1W",  # weeks unsupported
        "PT1H30",  # trailing number without unit
        "abcms",
        "-5s",  # negative shorthand rejected
        "-1",  # negative bare number rejected
        "-PT5S",  # negative ISO rejected
        "inf",
        "-inf",
        "nan",
        "Infinity",
    ],
)
def test_duration_corpus_rejects_and_returns_default(raw: str) -> None:
    cfg = Configuration(env=_env({"T": raw}))
    assert cfg.get_duration("T", 9.0) == 9.0
