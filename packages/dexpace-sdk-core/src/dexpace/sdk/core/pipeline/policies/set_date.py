# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that stamps each outgoing request with a fresh ``Date`` header."""

from __future__ import annotations

from email.utils import formatdate
from typing import TYPE_CHECKING, ClassVar, Literal

from ...util.clock import SYSTEM_CLOCK, Clock
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext


class SetDatePolicy(Policy):
    """Stamps the outgoing request with a ``Date`` header in RFC 7231 form.

    The header is produced via ``email.utils.formatdate(timestamp, usegmt=True)``
    so the rendering matches the canonical ``Sun, 06 Nov 1994 08:49:37 GMT``
    shape. Any caller-supplied ``Date`` header is overwritten so the value
    on the wire always reflects the moment the request leaves this policy.

    The policy is placed at `Stage.POST_RETRY` so each retry attempt
    receives a fresh timestamp. Stamping earlier — outside the retry
    wrapper — would cache the time across attempts and risk false
    signatures on services that bind the date into request signing
    (notably AWS SigV4 and similar).

    Attributes:
        STAGE: Pinned to `Stage.POST_RETRY` at the type level so
            mis-slotting is caught by ``mypy``.

    Example:
        ```python
        Pipeline(transport, policies=[RetryPolicy(), SetDatePolicy()])
        ```
    """

    STAGE: ClassVar[Literal[Stage.POST_RETRY]] = Stage.POST_RETRY
    __slots__ = ("_clock",)

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None:
        """Build the policy.

        Args:
            clock: Source of wall-clock time. Defaults to the process-wide
                `SYSTEM_CLOCK`; tests substitute a deterministic fake.
        """
        self._clock = clock

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        """Stamp ``request`` with a fresh ``Date`` header and dispatch.

        Args:
            request: Outgoing request. A new request is returned (the
                original is left untouched per the immutability contract).
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        stamped = request.with_header("Date", formatdate(self._clock.now(), usegmt=True))
        return self.next.send(stamped, ctx)


__all__ = ["SetDatePolicy"]
