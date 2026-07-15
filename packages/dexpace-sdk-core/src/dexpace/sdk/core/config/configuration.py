# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Layered runtime configuration with override + env-var + property lookup.

``Configuration`` provides a small, dependency-free way for SDK consumers to
tune runtime behaviour (retry caps, proxy URLs, default timeouts) without
threading kwargs through every constructor. Lookup is layered, highest
precedence first:

1. Explicit ``overrides`` passed to the constructor or set via the builder.
2. The injected env source (defaults to ``os.environ.get``), keyed by the
   exact name. Empty strings from the env source are treated as absent — this
   matches the Java SDK's behaviour where ``System.getenv`` returning ``""``
   is considered "not configured".
3. The injected property source (defaults to a no-op that resolves nothing),
   keyed by a dotted-lowercase normalisation of the name — for example the
   env-style ``MAX_RETRY_ATTEMPTS`` becomes the property-style
   ``max.retry.attempts``. The ``get_raw`` accessor variant skips this
   normalisation and queries the property source with the name verbatim. Like
   the env source, an empty property value is treated as absent.
4. The caller-supplied default.

Typed accessors (``get_int`` / ``get_bool`` / ``get_duration``) resolve
through the full layered lookup above and return the default on any parse
failure. They never raise at the lookup site — bad configuration should
degrade, not crash.

Built configurations are immutable; the override map is copied at construction
so a caller cannot mutate a live configuration. Derive a modified copy with
``with_override`` (copy-on-write — the derived configuration shares the
source's env and property seams); ``without_override`` peels a single key back
off the override layer so lookups fall through to env / property / default
again. A process-wide default slot is available via
``default_configuration`` / ``set_default_configuration`` with last-write-wins
semantics. Environment and property sources are substitutable objects: tests
inject fakes rather than mutating ``os.environ``.
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar, Final, Self

__all__ = [
    "Configuration",
    "ConfigurationBuilder",
    "default_configuration",
    "set_default_configuration",
]


EnvSource = Callable[[str], str | None]
PropertySource = Callable[[str], str | None]


def _no_properties(name: str) -> str | None:
    """Default property source: resolve nothing.

    Args:
        name: The (normalised) property key. Ignored.

    Returns:
        Always ``None`` — no injected property source is configured.
    """
    return None


def _to_property_key(name: str) -> str:
    """Normalise an env-style key to a dotted-lowercase property key.

    Underscores become dots and the whole key is lower-cased, so the
    env-style ``MAX_RETRY_ATTEMPTS`` maps to the property-style
    ``max.retry.attempts``.

    Args:
        name: The configuration key in any case.

    Returns:
        The dotted-lowercase property-key form.
    """
    return name.replace("_", ".").lower()


def _require_name(name: str) -> None:
    """Reject a ``None`` configuration key with a clear error.

    Args:
        name: The candidate key. Typed ``str``, but guarded at runtime so an
            untyped caller passing ``None`` fails loudly rather than deep in
            the lookup.

    Raises:
        TypeError: If ``name`` is ``None``.
    """
    if name is None:
        raise TypeError("configuration key must not be None")


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
    """Layered runtime configuration: override -> env -> property -> default.

    Attributes:
        overrides: Explicit string-keyed overrides that always win.
        env: Callable mapping a name to its environment value or ``None``.
            Defaults to ``os.environ.get``. Substitutable — inject a fake in
            tests instead of mutating ``os.environ``.
        props: Callable mapping a dotted-lowercase property key to its value
            or ``None``. Defaults to a no-op source. Substitutable, and
            queried under key normalisation by ``get`` (verbatim by
            ``get_raw``).
    """

    overrides: dict[str, str] = field(default_factory=dict)
    env: EnvSource = field(default=os.environ.get, repr=False)
    props: PropertySource = field(default=_no_properties, repr=False)

    def __post_init__(self) -> None:
        # Defensive copy: ``frozen=True`` protects the attribute binding, not
        # the dict's contents. Without this, external callers can mutate
        # ``overrides`` after construction and observe the change through
        # ``get``.
        object.__setattr__(self, "overrides", dict(self.overrides))

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
        """Resolve ``name`` honouring override -> env -> property -> default.

        The property source is queried under dotted-lowercase key
        normalisation (``MAX_RETRY_ATTEMPTS`` -> ``max.retry.attempts``); use
        ``get_raw`` to query it with the name verbatim.

        Args:
            name: Configuration key.
            default: Value to return when no layer supplies one.

        Returns:
            The resolved value or ``default``. Empty env / property strings are
            treated as absent; an empty override is honoured as an intentional
            value.
        """
        _require_name(name)
        return self._resolve(name, _to_property_key(name), default)

    def get_raw(self, name: str, default: str | None = None) -> str | None:
        """Resolve ``name`` like ``get`` but query the property source verbatim.

        The override and env layers are keyed by the exact name (as in
        ``get``); only the property layer differs — this accessor skips the
        dotted-lowercase normalisation and looks the name up unchanged.

        Args:
            name: Configuration key, used verbatim against every layer.
            default: Value to return when no layer supplies one.

        Returns:
            The resolved value or ``default``.
        """
        _require_name(name)
        return self._resolve(name, name, default)

    def _resolve(self, name: str, property_key: str, default: str | None) -> str | None:
        """Run the layered lookup for ``name`` against a chosen property key.

        Args:
            name: Key used for the override and env layers.
            property_key: Key used for the property layer (normalised by
                ``get``, verbatim by ``get_raw``).
            default: Fallback when no layer resolves a non-empty value.

        Returns:
            The first resolved value, or ``default``.
        """
        if name in self.overrides:
            return self.overrides[name]
        env_value = self.env(name)
        if env_value:
            return env_value
        prop_value = self.props(property_key)
        if prop_value:
            return prop_value
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
            non-finite (``inf`` / ``nan``) / negative result.
        """
        raw = self.get(name)
        if raw is None:
            return default_seconds
        parsed = _parse_duration_seconds(raw)
        if parsed is None or not math.isfinite(parsed) or parsed < 0:
            return default_seconds
        return parsed

    # ------------------------------------------------------------------
    # Derivation (copy-on-write)

    def with_override(self, name: str, value: str) -> Configuration:
        """Return a copy with ``name`` set to ``value`` in the override layer.

        Copy-on-write: the source is left untouched and the derived
        configuration shares the source's env and property seams (same
        objects), so a single injected fake seam drives both.

        Args:
            name: Override key to set.
            value: Override value that wins over env / property / default.

        Returns:
            A new ``Configuration`` carrying the merged overrides.
        """
        _require_name(name)
        merged = dict(self.overrides)
        merged[name] = value
        return Configuration(overrides=merged, env=self.env, props=self.props)

    def without_override(self, name: str) -> Configuration:
        """Return a copy with ``name`` dropped from the override layer.

        Removing an override peels off only that one layer: a later lookup for
        ``name`` falls through to env -> property -> default exactly as if the
        override had never been set. Removal never forces the key to resolve to
        ``None`` — a lower layer still supplies its value. Removing a key that
        carries no override is a harmless no-op that returns an equivalent
        copy. Copy-on-write, like ``with_override``: the source is left
        untouched and the derived configuration shares the source's env and
        property seams (same objects).

        Args:
            name: Override key to drop from the override layer.

        Returns:
            A new ``Configuration`` with ``name`` absent from the overrides.
        """
        _require_name(name)
        merged = dict(self.overrides)
        merged.pop(name, None)
        return Configuration(overrides=merged, env=self.env, props=self.props)


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

    The builder is single-threaded by contract: a single builder instance must
    not be mutated concurrently from multiple threads. Build once, then share
    the immutable ``Configuration``, which is safe to read concurrently.
    """

    __slots__ = ("_env", "_overrides", "_props")

    _overrides: dict[str, str]
    _env: EnvSource
    _props: PropertySource

    def __init__(self) -> None:
        """Initialise with empty overrides, ``os.environ.get`` env, no props."""
        self._overrides = {}
        self._env = os.environ.get
        self._props = _no_properties

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

    def props(self, source: PropertySource) -> Self:
        """Replace the property source.

        Args:
            source: Callable that maps a (normalised) property key to its
                value or ``None``.

        Returns:
            ``self`` for chaining.
        """
        self._props = source
        return self

    def build(self) -> Configuration:
        """Snapshot the builder into an immutable ``Configuration``.

        Returns:
            A frozen ``Configuration`` carrying a copy of the overrides and the
            configured env / property seams.
        """
        return Configuration(overrides=dict(self._overrides), env=self._env, props=self._props)


# ----------------------------------------------------------------------
# Process-wide default slot (last-write-wins)

_default_lock: Final = threading.Lock()
_default_configuration: Configuration | None = None


def default_configuration() -> Configuration:
    """Return the process-wide default configuration.

    Lazily initialises to an empty ``Configuration`` (env-only lookup) on
    first access if none has been set. Thread-safe.

    Returns:
        The current default ``Configuration``.
    """
    global _default_configuration
    with _default_lock:
        current = _default_configuration
        if current is None:
            current = Configuration()
            _default_configuration = current
        return current


def set_default_configuration(config: Configuration) -> None:
    """Install ``config`` as the process-wide default (last-write-wins).

    Args:
        config: The configuration to publish as the default. A later call
            replaces an earlier one.

    Raises:
        TypeError: If ``config`` is ``None``.
    """
    if config is None:
        raise TypeError("default configuration must not be None")
    global _default_configuration
    with _default_lock:
        _default_configuration = config
