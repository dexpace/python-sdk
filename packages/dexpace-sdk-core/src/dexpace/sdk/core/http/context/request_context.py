# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Second link in the context promotion chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...instrumentation import InstrumentationContext
from .call_context import CallContext

if TYPE_CHECKING:
    from ..request.request import Request
    from ..response.async_response import AsyncResponse
    from ..response.response import Response
    from .exchange_context import ExchangeContext


@dataclass(frozen=True)
class RequestContext(CallContext):
    """Second link in the context promotion chain.

    Adds the outgoing `Request` to the call's
    `InstrumentationContext`. Once a `Response` arrives,
    `to_exchange_context` promotes this into an `ExchangeContext`.
    """

    instrumentation_context: InstrumentationContext
    request: Request

    def to_exchange_context(
        self,
        response: Response | AsyncResponse,
    ) -> ExchangeContext:
        """Promote into an `ExchangeContext` bound to ``response``.

        Stores the new context in `ContextStore` keyed by trace id.
        """
        from .context_store import ContextStore
        from .exchange_context import ExchangeContext

        promoted = ExchangeContext(
            instrumentation_context=self.instrumentation_context,
            request=self.request,
            response=response,
        )
        ContextStore.set(promoted.instrumentation_context.trace_id.value, promoted)
        return promoted


__all__ = ["RequestContext"]
