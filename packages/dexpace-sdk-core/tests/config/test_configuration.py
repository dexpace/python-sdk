"""Tests for ``Configuration`` — layered override + env-var lookup."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.config import Configuration, ConfigurationBuilder


def _env(mapping: dict[str, str]):  # type: ignore[no-untyped-def]
    """Build a fake env source closing over ``mapping``."""

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


def test_get_duration_parse_failure_returns_default() -> None:
    cfg = Configuration(env=_env({"T": "nonsense"}))
    assert cfg.get_duration("T", 9.0) == 9.0


def test_get_duration_missing_returns_default() -> None:
    cfg = Configuration(env=_env({}))
    assert cfg.get_duration("T", 9.0) == 9.0


def test_get_duration_negative_returns_default() -> None:
    cfg = Configuration(env=_env({"T": "-5s"}))
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


def test_default_env_is_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPACE_TEST_CONFIG_KEY", "value-from-os")
    cfg = Configuration()
    assert cfg.get("DEXPACE_TEST_CONFIG_KEY", None) == "value-from-os"
