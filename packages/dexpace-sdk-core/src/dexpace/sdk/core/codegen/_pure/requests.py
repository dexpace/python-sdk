# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure request-binding planner shared by both executor shells.

`plan_request` is the single seam both `ServiceCore` and `AsyncServiceCore`
call to turn a declarative operation plus its per-call input into a concrete
`Request`. It delegates to the certified `assemble` (PY-M9-01) rather than
restating any path / query / header logic; keeping it here — free of any
awaiting and of either executor shell — lets the sync and async twins share one
definition of request binding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..assembly import assemble

if TYPE_CHECKING:
    from ...http.common import Url
    from ...http.request import Request
    from ...serde import Serde
    from ..operation import Operation, OperationInput

__all__ = ["plan_request"]


def plan_request(
    op: Operation,
    input: OperationInput,
    base_url: str | Url,
    serde: Serde | None,
) -> Request:
    """Bind an operation and its per-call input into a concrete `Request`.

    The executor's request-binding step. It is a thin, awaiting-free wrapper
    over `assemble`, so both executor shells produce byte-identical requests
    from the same inputs; a generated caller's parity check therefore compares
    two shells that share this one planner rather than two open-coded builders.

    Args:
        op: The static operation description (method, path template, headers).
        input: The per-call values (path params, query, headers, body).
        base_url: The absolute base URL, as a string or `Url`.
        serde: Body serializer reserved for a later executor layer; forwarded
            to `assemble` unchanged (the assembly layer carries the body
            through verbatim and does not consult it yet).

    Returns:
        The assembled `Request`.

    Raises:
        KeyError: If the path template names a placeholder with no matching
            entry in ``input.path_params``.
        ValueError: If ``base_url`` carries a URL fragment or is not a valid
            absolute URL.
    """
    return assemble(op, input, base_url, serde=serde)
