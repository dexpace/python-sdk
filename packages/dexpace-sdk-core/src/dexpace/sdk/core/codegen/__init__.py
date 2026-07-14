# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tier-1 declarative request-assembly contract and platform executors.

The `Operation` / `OperationInput` value objects describe a request the way a
generated SDK client would, and `assemble` turns that declaration into a
concrete `Request` (method, URL, headers). `StatusErrorMap` is the response-side
companion: a frozen status-code → typed-error mapping.

`ServiceCore` / `AsyncServiceCore` are the two platform executors a generated
SDK facade delegates to. Each exposes `execute` / `execute_request` /
`paginate` / `events`, and both delegate every non-awaiting decision to the same
shared pure planners so only the awaiting shell differs between the sync and
async twins. `AsyncResponseHandler` / `AsyncDecodeSuccess` are the async twin of
serde's `ResponseHandler` / `DecodeSuccess`, used as the async executor's
default handler.
"""

from __future__ import annotations

from .assembly import assemble
from .async_response_handler import AsyncDecodeSuccess, AsyncResponseHandler
from .async_service_core import AsyncServiceCore
from .operation import Operation, OperationInput
from .service_core import ServiceCore
from .status_error_map import StatusErrorMap

__all__ = [
    "AsyncDecodeSuccess",
    "AsyncResponseHandler",
    "AsyncServiceCore",
    "Operation",
    "OperationInput",
    "ServiceCore",
    "StatusErrorMap",
    "assemble",
]
