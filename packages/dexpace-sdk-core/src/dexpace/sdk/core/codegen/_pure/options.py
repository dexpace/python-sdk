# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure per-call options planner shared by both executor shells.

`plan_options` packs a call's per-call keyword overrides — plus an optional
per-call auth override — into a single frozen mapping. The executor splats that
mapping into ``Pipeline.run(**options)`` / ``AsyncPipeline.run(**options)``,
which copies it once into the mutable ``PipelineContext.options`` that stays the
*same object* across every retry / redirect re-drive (the identity guarantee
PY-M3-05 certified). Freezing the planner's output keeps the caller from
mutating the mapping after the fact.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["AUTH_OPTION_KEY", "plan_options"]

#: Reserved key under which a per-call ``auth`` override is parked in the
#: options mapping. It is threaded through the pipeline verbatim so a future
#: auth-requirement policy (PY-M9-05) can read it; this task only parks it.
AUTH_OPTION_KEY: Final = "auth"


def plan_options(auth: object | None, call_options: Mapping[str, Any]) -> Mapping[str, Any]:
    """Pack per-call options and an optional auth override into a frozen mapping.

    Args:
        auth: An optional per-call authentication override. When not ``None`` it
            is parked under `AUTH_OPTION_KEY`; it is threaded through the
            pipeline unchanged, not consumed here.
        call_options: The caller's per-call keyword overrides, forwarded to the
            pipeline's policies via ``ctx.options``.

    Returns:
        A read-only mapping of the merged options. The executor splats it into
        ``Pipeline.run``; a per-call ``auth`` key set here takes precedence over
        one of the same name in ``call_options``.
    """
    merged: dict[str, Any] = dict(call_options)
    if auth is not None:
        merged[AUTH_OPTION_KEY] = auth
    return MappingProxyType(merged)
