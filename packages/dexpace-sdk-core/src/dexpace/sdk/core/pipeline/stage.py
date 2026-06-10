# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline stage taxonomy for the staged-builder API.

Each `Policy` declares its `STAGE: ClassVar[Stage]`. `StagedPipelineBuilder`
orders policies by stage rather than by caller-specified order, removing a
class of bugs where retry runs before redirect or auth runs after logging.

Pillar stages admit at most one policy; non-pillar stages stack with
deque semantics. The numeric values are spaced out so future
stages can slot between existing ones without renumbering.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class Stage(IntEnum):
    """Pipeline stage ordering. Lower value runs first (closer to caller entry).

    Stages divide into four groups:

    - **Operation** (`OPERATION`): runs *outside* the redirect / retry
      wrappers, so a single entry brackets the whole operation regardless of
      how many hops or attempts happen inside. This is where per-operation
      lifecycle observation (e.g. `OperationTracingPolicy`) belongs — events
      that must fire exactly once and reflect the final outcome.
    - **Wrapping** (`REDIRECT`, `RETRY`): re-invoke the downstream chain per
      hop / attempt. Their pillar slot is reserved for the single redirect /
      retry policy. `POST_*` siblings run *inside* the wrapper's loop.
    - **Auth** (`PRE_AUTH`, `AUTH`, `POST_AUTH`): credential application.
      `AUTH` is the pillar — only one credential policy at a time.
    - **Instrumentation** (`PRE_LOGGING`, `LOGGING`, `POST_LOGGING`): logging
      and tracing. `LOGGING` is the pillar (typically `LoggingPolicy`);
      `TracingPolicy` sits at `POST_LOGGING` so it observes the trace id
      stamped by logging.
    - **Serde / send** (`PRE_SERDE`, `SERDE`, `POST_SERDE`, `PRE_SEND`,
      `SEND`): body-to-bytes (`SERDE` reserved, currently unused) and the
      terminal transport call (`SEND` — never a user-step slot).
    """

    OPERATION = 50

    REDIRECT = 100
    POST_REDIRECT = 150
    RETRY = 200
    POST_RETRY = 250

    PRE_AUTH = 300
    AUTH = 400
    POST_AUTH = 500

    PRE_LOGGING = 600
    LOGGING = 700
    POST_LOGGING = 800

    PRE_SERDE = 900
    SERDE = 1000
    POST_SERDE = 1100
    PRE_SEND = 1200
    SEND = 1300

    @property
    def is_pillar(self) -> bool:
        """True if this stage admits at most one policy (no stacking)."""
        return self in _PILLARS


_PILLARS: Final[frozenset[Stage]] = frozenset(
    {
        Stage.OPERATION,
        Stage.REDIRECT,
        Stage.RETRY,
        Stage.AUTH,
        Stage.LOGGING,
        Stage.SERDE,
        Stage.SEND,
    }
)


__all__ = ["Stage"]
