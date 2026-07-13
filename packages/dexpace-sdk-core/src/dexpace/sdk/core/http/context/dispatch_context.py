# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Entry point of the context promotion chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from ...instrumentation import NOOP_INSTRUMENTATION_CONTEXT, InstrumentationContext
from .call_context import CallContext

if TYPE_CHECKING:
    from ..request.request import Request
    from .request_context import RequestContext


@dataclass(frozen=True)
class DispatchContext(CallContext):
    """First link in the context promotion chain.

    Carries only the `InstrumentationContext`. Once a `Request`
    has been built, `to_request_context` promotes this into a
    `RequestContext`. The promotion produces a new immutable instance
    and registers it with `ContextStore` under a process-unique key so
    downstream observers see the latest snapshot.

    A dispatch context is not itself registered, so its `store_key` stays
    ``None`` until it is promoted.
    """

    instrumentation_context: InstrumentationContext
    store_key: str | None = None

    def to_request_context(self, request: Request) -> RequestContext:
        """Promote into a `RequestContext` bound to ``request``.

        Mints a process-unique key (``{trace_id}:{span_id}:{n}``) and stores
        the new context in `ContextStore` under it, so two concurrent calls
        that share a trace id do not collide.
        """
        from .context_store import ContextStore
        from .request_context import RequestContext

        instrumentation = self.instrumentation_context
        key = ContextStore.new_key(
            instrumentation.trace_id.value,
            instrumentation.span_id.value,
        )
        promoted = RequestContext(
            instrumentation_context=instrumentation,
            request=request,
            store_key=key,
        )
        ContextStore.set(key, promoted)
        return promoted

    @classmethod
    def noop(cls) -> Self:
        """Dispatch context with a no-op instrumentation context.

        Used when tracing is disabled.
        """
        return cls(NOOP_INSTRUMENTATION_CONTEXT)


__all__ = ["DispatchContext"]
