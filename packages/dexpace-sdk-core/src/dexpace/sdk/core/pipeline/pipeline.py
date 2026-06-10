# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Composable HTTP pipeline — context-manager wrapper around an ordered policy chain."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from ._sansio_runner import _SansIORequestRunner, _SansIOResponseRunner
from ._transport_runner import _TransportRunner
from .context import PipelineContext
from .policy import Policy

if TYPE_CHECKING:
    from ..client.http_client import HttpClient
    from ..http.context.call_context import CallContext
    from ..http.context.dispatch_context import DispatchContext
    from ..http.request.request import Request
    from ..http.response.response import Response

#: Member of the ``policies`` list passed to ``Pipeline``. Either a full
#: ``Policy`` (with ``.next`` chaining) or a SansIO callable on either side.
#: ``Callable`` return types are structurally covariant in mypy, so a step
#: returning ``Request`` is accepted in the ``Request | None`` slot.
type _Step = (
    Policy
    | Callable[[Request, CallContext], Request | None]
    | Callable[[Response, CallContext], Response | None]
)


class Pipeline:
    """Composes an ordered sequence of policies around an ``HttpClient``.

    The pipeline is the public entry point most consumers use::

        with Pipeline(transport, policies=[retry, auth, logger]) as p:
            response = p.run(request, dispatch_ctx)

    SansIO steps in the list are auto-wrapped depending on whether they have
    a ``request_side`` or ``response_side`` attribute (set on the callable
    by the SDK's built-in step decorators) — by default the runner assumes a
    request-side step. For policies that need explicit chain control
    (retry, auth challenges), implement the ``Policy`` ABC directly.

    Attributes:
        transport: The terminal HTTP client.
    """

    __slots__ = ("_chain", "transport")

    def __init__(
        self,
        transport: HttpClient,
        policies: Sequence[_Step] | None = None,
    ) -> None:
        """Construct the chain.

        Args:
            transport: The terminal HTTP client.
            policies: In-order list of policies / SansIO steps. The first
                policy is invoked first; subsequent policies are reached via
                ``self.next``. A terminal ``_TransportRunner`` is appended
                automatically.

        Raises:
            TypeError: If an entry in ``policies`` is neither a ``Policy``
                nor a callable matching the SansIO step shape.
        """
        self.transport = transport
        wrapped: list[Policy] = [
            entry if isinstance(entry, Policy) else _wrap_step(entry) for entry in (policies or [])
        ]
        for i, policy in enumerate(wrapped[:-1]):
            policy.next = wrapped[i + 1]
        terminal = _TransportRunner(transport)
        if wrapped:
            wrapped[-1].next = terminal
        self._chain: Policy = wrapped[0] if wrapped else terminal

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def run(
        self,
        request: Request,
        dispatch: DispatchContext,
        **options: Any,
    ) -> Response:
        """Send ``request`` through the chain and return its response.

        Args:
            request: The HTTP request to send.
            dispatch: Per-call telemetry context (typically built by the
                caller's tracing layer). Promoted internally to a
                ``RequestContext`` before policies run.
            **options: Caller-supplied per-call overrides exposed to
                policies via ``ctx.options``.

        Returns:
            The response from the terminal transport.
        """
        request_ctx = dispatch.to_request_context(request)
        ctx = PipelineContext(call=request_ctx, options=dict(options))
        try:
            return self._chain.send(request, ctx)
        finally:
            # Evict the call's ``ContextStore`` entry once the chain has fully
            # unwound — in-chain observers have already read the latest tier.
            # The exchange context shares this trace id, so a single close
            # clears both tiers and prevents unbounded growth across calls.
            request_ctx.close()


def _wrap_step(step: Any) -> Policy:
    """Wrap a SansIO step in the right runner Policy.

    Steps are tagged via the ``side`` attribute (``"request"`` /
    ``"response"``) for explicit dispatch. Untagged callables default to the
    request side, matching the common case (header stamping, redaction).
    """
    if not callable(step):
        raise TypeError(f"Pipeline step {step!r} is neither a Policy nor a callable.")
    side = getattr(step, "side", "request")
    if side == "response":
        return _SansIOResponseRunner(step)
    return _SansIORequestRunner(step)


__all__ = ["Pipeline"]
