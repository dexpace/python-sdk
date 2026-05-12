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

    Carries only the :class:`InstrumentationContext`. Once a :class:`Request`
    has been built, :meth:`to_request_context` promotes this into a
    :class:`RequestContext`. The promotion produces a new immutable instance
    and re-registers it with :data:`ContextStore` under the same trace id so
    downstream observers see the latest snapshot.
    """

    instrumentation_context: InstrumentationContext

    def to_request_context(self, request: Request) -> RequestContext:
        """Promote into a :class:`RequestContext` bound to ``request``.

        Stores the new context in :data:`ContextStore` keyed by trace id.
        """
        from .context_store import ContextStore
        from .request_context import RequestContext

        promoted = RequestContext(
            instrumentation_context=self.instrumentation_context,
            request=request,
        )
        ContextStore.set(promoted.instrumentation_context.trace_id.value, promoted)
        return promoted

    @classmethod
    def noop(cls) -> Self:
        """Dispatch context with a no-op instrumentation context.

        Used when tracing is disabled.
        """
        return cls(NOOP_INSTRUMENTATION_CONTEXT)


__all__ = ["DispatchContext"]
