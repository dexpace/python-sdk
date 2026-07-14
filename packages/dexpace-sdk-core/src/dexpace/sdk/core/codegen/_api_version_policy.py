# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Private sync pipeline policy that stamps a constant api-version header.

Internal to `merge_config`'s pipeline flattening — not part of the codegen
public surface (hence the leading-underscore module name; the class itself
keeps a normal name so `Policy.__init_subclass__` still enforces its `STAGE`
declaration).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ..pipeline.policy import Policy
from ..pipeline.stage import Stage

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.response import Response
    from ..pipeline.context import PipelineContext

__all__ = ["ApiVersionPolicy"]


class ApiVersionPolicy(Policy):
    """Stamps every outgoing request with a constant api-version header.

    Placed at `Stage.PRE_SEND` — the innermost non-terminal stage, sitting
    inside both the redirect (`Stage.REDIRECT`) and retry (`Stage.RETRY`)
    wrappers — so the header is re-applied on every hop and every attempt
    rather than minted once and silently carried (or dropped) across a
    reissued request.

    Attributes:
        STAGE: Pinned to `Stage.PRE_SEND` at the type level so mis-slotting
            is caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.PRE_SEND]] = Stage.PRE_SEND
    __slots__ = ("_header", "_value")

    def __init__(self, header: str, value: str) -> None:
        """Build the policy.

        Args:
            header: The header name the api-version value is stamped under.
            value: The api-version value stamped on every request.
        """
        self._header = header
        self._value = value

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        """Stamp ``request`` with the configured api-version header and dispatch.

        Args:
            request: Outgoing request. A new request is returned (the
                original is left untouched per the immutability contract).
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        return self.next.send(request.with_header(self._header, self._value), ctx)
