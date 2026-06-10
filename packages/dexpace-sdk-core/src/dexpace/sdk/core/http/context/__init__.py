# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Per-call context promotion chain.

A call moves through three immutable context shapes as it executes:

1. `DispatchContext` — carries only the `InstrumentationContext`;
   created before a request payload exists.
2. `RequestContext` — adds the outgoing `Request`; produced by
   `DispatchContext.to_request_context` once a request has been built.
3. `ExchangeContext` — adds the `Response`; produced by
   `RequestContext.to_exchange_context` once a response has arrived.

Each promotion is registered with `ContextStore` under the call's trace
id so downstream observers see the latest snapshot for a call by trace id.
"""

from __future__ import annotations

from .call_context import CallContext
from .context_store import ContextStore
from .dispatch_context import DispatchContext
from .exchange_context import ExchangeContext
from .request_context import RequestContext

__all__ = [
    "CallContext",
    "ContextStore",
    "DispatchContext",
    "ExchangeContext",
    "RequestContext",
]
