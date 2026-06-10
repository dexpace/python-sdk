# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Optional configuration objects layered onto pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StepMetadata:
    """Human-readable identification for a pipeline step.

    Used by logging, tracing, and tooling that needs to identify a step at
    runtime. Bump `version` when behavior changes in a non
    back-compatible way.
    """

    name: str
    description: str = ""
    version: str = ""
    tags: tuple[str, ...] = ()


__all__ = ["StepMetadata"]
