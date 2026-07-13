# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Build descriptor: the running SDK distribution's name and version.

``BuildDescriptor`` reports the installed distribution name and version so
observability (``User-Agent`` strings, span attributes, log context) can stamp
"which build produced this". The version is read from installed package
metadata via ``importlib.metadata``. That lookup can fail under zipapp / frozen
/ source-tree installs where distribution metadata is absent — in that case the
descriptor falls back to a non-blank sentinel (``"unknown"``) rather than
raising or silently producing a blank string.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import ClassVar, Final, Self

__all__ = ["BuildDescriptor"]

_CORE_DISTRIBUTION: Final[str] = "dexpace-sdk-core"


@dataclass(frozen=True, slots=True)
class BuildDescriptor:
    """Immutable descriptor of a distribution's build identity.

    Attributes:
        name: The distribution name (never blank).
        version: The resolved version, or ``UNKNOWN`` when metadata is
            unavailable or blank (never blank).
    """

    name: str
    version: str

    UNKNOWN: ClassVar[str] = "unknown"

    @classmethod
    def resolve(cls, distribution: str = _CORE_DISTRIBUTION) -> Self:
        """Resolve the descriptor for ``distribution`` from package metadata.

        The version is read via ``importlib.metadata.version``. When the
        distribution is not installed (``PackageNotFoundError``) or metadata
        reports a blank version, the ``UNKNOWN`` sentinel is substituted so the
        descriptor is always non-blank.

        Args:
            distribution: The distribution name to resolve. Defaults to the
                SDK core distribution.

        Returns:
            A populated ``BuildDescriptor`` — both fields are guaranteed
            non-blank.
        """
        name = distribution.strip() or cls.UNKNOWN
        try:
            resolved = version(distribution)
        except PackageNotFoundError:
            resolved = ""
        if not resolved.strip():
            resolved = cls.UNKNOWN
        return cls(name=name, version=resolved)
