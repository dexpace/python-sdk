# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure planner layer for the codegen executors.

Dependency-light planning functions that carry no awaiting and no dependency
on either executor shell, so the sync `ServiceCore` and async
`AsyncServiceCore` twins import the single canonical definition instead of
restating it. Only the awaiting shell (sync vs. async) differs between the two
executors; every non-awaiting decision they share lives here.

Keeping these modules shell-free is what lets the import-linter layering
contract place them below the two executor modules and forbid the executor
shells from importing one another.

Modules:
    requests: request binding and assembly (delegates to `assemble`).
    options: packing per-call options (and per-call auth) into the frozen
        options mapping threaded through the pipeline.
    results: the raw-response-vs-handler decision and response-witness
        resolution shared by both executors' result handling.
"""

from __future__ import annotations

__all__: list[str] = []
