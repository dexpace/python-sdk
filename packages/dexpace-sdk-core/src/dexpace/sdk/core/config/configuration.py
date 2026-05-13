"""Layered runtime configuration with override + env-var lookup.

``Configuration`` provides a small, dependency-free way for SDK consumers to
tune runtime behaviour (retry caps, proxy URLs, default timeouts) without
threading kwargs through every constructor. Lookup is layered:

1. Explicit ``overrides`` passed to the constructor or set via the builder.
2. The injected env source (defaults to ``os.environ.get``).
   Empty strings from the env source are treated as absent — this matches
   the Java SDK's behaviour where ``System.getenv`` returning ``""`` is
   considered "not configured".
3. The caller-supplied default.

Typed accessors (``get_int`` / ``get_bool`` / ``get_duration``) return the
default on any parse failure. They never raise at the lookup site — bad
configuration should degrade, not crash.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar, Final, Self

__all__ = ["Configuration", "ConfigurationBuilder"]


EnvSource = Callable[[str], str | None]


_SHORTHAND_RE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$", re.IGNORECASE)
_SHORTHAND_MULTIPLIERS: Final[dict[str, float]] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}
_ISO_RE: Final = re.compile(
    r"^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
)


@dataclass(frozen=True, slots=True)
class Configuration:
    """Layered runtime configuration: override -> env var -> default.

    Attributes:
        overrides: Explicit string-keyed overrides that always win.
        env: Callable mapping a name to its environment value or ``None``.
            Defaults to ``os.environ.get``.
    """

    overrides: dict[str, str] = field(default_factory=dict)
    env: EnvSource = field(default=os.environ.get, repr=False)

    # Well-known keys — mirror Java SDK constants where they apply.
    # ``ClassVar`` keeps these off the dataclass field list and out of ``__slots__``.
    MAX_RETRY_ATTEMPTS: ClassVar[str] = "MAX_RETRY_ATTEMPTS"
    REQUEST_RETRY_DEFAULT_TIMEOUT: ClassVar[str] = "REQUEST_RETRY_DEFAULT_TIMEOUT"
    HTTPS_PROXY: ClassVar[str] = "HTTPS_PROXY"
    HTTP_PROXY: ClassVar[str] = "HTTP_PROXY"
    NO_PROXY: ClassVar[str] = "NO_PROXY"

    # ------------------------------------------------------------------
    # Construction helpers

    @classmethod
    def builder(cls) -> ConfigurationBuilder:
        """Return a fresh fluent builder.

        Returns:
            A new ``ConfigurationBuilder``.
        """
        return ConfigurationBuilder()

    # ------------------------------------------------------------------
    # Lookup

    def get(self, name: str, default: str | None = None) -> str | None:
        """Resolve ``name`` honouring override -> env -> default ordering.

        Args:
            name: Configuration key.
            default: Value to return when neither override nor env supplies one.

        Returns:
            The resolved value or ``default``. Empty env strings are treated
            as absent; an empty override is honoured as an intentional value.
        """
        if name in self.overrides:
            return self.overrides[name]
        env_value = self.env(name)
        if env_value is not None and env_value != "":
            return env_value
        return default

    def get_int(self, name: str, default: int) -> int:
        """Resolve ``name`` as an integer.

        Args:
            name: Configuration key.
            default: Returned when the key is absent or unparseable.

        Returns:
            The parsed integer, or ``default`` on miss / parse failure.
        """
        raw = self.get(name)
        if raw is None:
            return default
        try:
            return int(raw.strip())
        except ValueError:
            return default

    def get_bool(self, name: str, default: bool) -> bool:
        """Resolve ``name`` as a strict boolean.

        Only the case-insensitive literals ``"true"`` and ``"false"`` are
        accepted. ``"1"`` / ``"yes"`` / ``"on"`` and friends are intentionally
        rejected — ambiguous truthiness causes more bugs than it solves.

        Args:
            name: Configuration key.
            default: Returned for missing or unrecognised values.

        Returns:
            ``True``/``False`` for recognised tokens; ``default`` otherwise.
        """
        raw = self.get(name)
        if raw is None:
            return default
        token = raw.strip().lower()
        if token == "true":
            return True
        if token == "false":
            return False
        return default

    def get_duration(self, name: str, default_seconds: float) -> float:
        """Resolve ``name`` as a duration in **seconds**.

        Accepted forms:

        - ISO-8601: ``PT5S``, ``PT1M``, ``PT2H``, ``P1D``, ``PT0.5S``.
        - Shorthand: ``500ms``, ``5s``, ``1m``, ``2h``, ``1d``.
        - Bare number: interpreted as **seconds** (note: the Java SDK
          interprets bare numbers as milliseconds; this port deliberately
          differs to match Python's ``float`` seconds convention used by
          ``time.sleep`` / ``Clock``).

        Args:
            name: Configuration key.
            default_seconds: Returned on miss or parse failure.

        Returns:
            Duration in seconds, or ``default_seconds`` on miss / failure /
            negative result.
        """
        raw = self.get(name)
        if raw is None:
            return default_seconds
        parsed = _parse_duration_seconds(raw)
        if parsed is None or parsed < 0:
            return default_seconds
        return parsed


def _parse_duration_seconds(raw: str) -> float | None:
    """Parse a duration string into seconds, returning ``None`` on failure.

    Args:
        raw: Candidate duration string.

    Returns:
        Seconds as a float, or ``None`` if no supported form matched.
    """
    text = raw.strip()
    if not text:
        return None
    iso = _parse_iso_8601(text)
    if iso is not None:
        return iso
    shorthand = _SHORTHAND_RE.match(text)
    if shorthand is not None:
        magnitude = float(shorthand.group(1))
        unit = shorthand.group(2).lower()
        return magnitude * _SHORTHAND_MULTIPLIERS[unit]
    try:
        return float(text)
    except ValueError:
        return None


def _parse_iso_8601(text: str) -> float | None:
    """Parse a minimal ISO-8601 duration into seconds.

    Supports day, hour, minute, second components (``P[nD][T[nH][nM][nS]]``).
    Weeks / months / years are intentionally unsupported — they are
    calendar-dependent and ill-defined for retry/timeout budgets.

    Args:
        text: Candidate ISO-8601 string.

    Returns:
        Seconds as a float, or ``None`` if the string did not match.
    """
    if not text.startswith("P"):
        return None
    match = _ISO_RE.match(text)
    if match is None:
        return None
    days, hours, minutes, seconds = match.groups()
    if days is None and hours is None and minutes is None and seconds is None:
        return None
    total = 0.0
    if days is not None:
        total += float(days) * 86400.0
    if hours is not None:
        total += float(hours) * 3600.0
    if minutes is not None:
        total += float(minutes) * 60.0
    if seconds is not None:
        total += float(seconds)
    return total


class ConfigurationBuilder:
    """Fluent builder for ``Configuration`` instances.

    Calls to ``put`` mutate the builder in place and return ``self`` so calls
    chain. ``build`` produces a frozen ``Configuration`` snapshot — further
    builder mutations do not affect already-built configurations.
    """

    __slots__ = ("_env", "_overrides")

    _overrides: dict[str, str]
    _env: EnvSource

    def __init__(self) -> None:
        """Initialise with empty overrides and ``os.environ.get`` as env."""
        self._overrides = {}
        self._env = os.environ.get

    def put(self, name: str, value: str) -> Self:
        """Set ``name`` to ``value`` in the override layer.

        Args:
            name: Configuration key.
            value: Override value (subsequent calls win).

        Returns:
            ``self`` for chaining.
        """
        self._overrides[name] = value
        return self

    def env(self, source: EnvSource) -> Self:
        """Replace the env source.

        Args:
            source: Callable that maps key -> value-or-None.

        Returns:
            ``self`` for chaining.
        """
        self._env = source
        return self

    def build(self) -> Configuration:
        """Snapshot the builder into an immutable ``Configuration``.

        Returns:
            A frozen ``Configuration`` carrying a copy of the overrides.
        """
        return Configuration(overrides=dict(self._overrides), env=self._env)
