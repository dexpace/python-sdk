# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure result-handling planner shared by both executor shells.

`plan_result` is the awaiting-free decision both `ServiceCore` and
`AsyncServiceCore` make once they hold a response: hand the raw response back,
or run a handler and, if so, which handler and against which response-type
witness. The executor shells then *apply* the chosen handler — the only
difference being that the async shell awaits it. The classification and decode
logic itself lives in the response handlers (`DecodeSuccess` / its async twin)
and the pure `classify_status` decision they share; this planner only routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...serde import TypeRef

__all__ = ["plan_result"]

#: A resolved response-type witness — a concrete class, a subscripted generic
#: (``list[Pet]``), or a validated `TypeRef` — as a response handler accepts.
type _Witness = type[Any] | TypeRef[Any]


def plan_result[H](
    handler: H | None,
    default_handler: H,
    response_type: _Witness | None,
    default_witness: _Witness,
) -> tuple[H, _Witness] | None:
    """Decide how a response becomes the caller's result.

    Args:
        handler: An explicit per-call handler, or ``None`` to use
            ``default_handler``.
        default_handler: The executor's default response handler (a
            `DecodeSuccess` for the sync shell, its async twin for the async
            shell).
        response_type: The requested result type, or ``None`` when the caller
            asked for no decoding.
        default_witness: The witness to pass a handler when ``response_type`` is
            ``None`` (the concrete response class of the shell — a handler that
            decodes nothing ignores it).

    Returns:
        ``None`` when the raw response should be returned unchanged — the case
        of no ``response_type`` and no ``handler``. Otherwise a
        ``(handler, witness)`` pair: the chosen handler and the witness to
        invoke it with. A per-call ``handler`` wins over the default; a given
        ``response_type`` wins over ``default_witness``.
    """
    if handler is None and response_type is None:
        return None
    chosen = default_handler if handler is None else handler
    witness = default_witness if response_type is None else response_type
    return chosen, witness
