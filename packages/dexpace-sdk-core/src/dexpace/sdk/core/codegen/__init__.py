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
default handler. `ServiceDefaults` plus `merge_config` / `merge_async_config`
are the Tier-2 seam: they fold a service's baked-in defaults and an optional
user override into the single core pipeline's constructor kwargs, never a
second pipeline or a wrapper client.

`AuthRequirement` / `AuthDescriptor` / `AuthRegistry` and the executors' tiered
resolver select the credential to apply per call by the most specific tier
present (per-call, then operation, then client), failing rather than falling
through when a present-but-unsatisfiable tier is selected.
"""

from __future__ import annotations

from .assembly import assemble
from .async_merge_config import merge_async_config
from .async_response_handler import AsyncDecodeSuccess, AsyncResponseHandler
from .async_service_core import AsyncServiceCore
from .auth import (
    AuthDescriptor,
    AuthRegistry,
    AuthRequirement,
    AuthResolutionError,
    AuthScheme,
)
from .merge_config import merge_config
from .operation import Operation, OperationInput
from .service_core import ServiceCore
from .service_defaults import ServiceDefaults
from .status_error_map import StatusErrorMap

__all__ = [
    "AsyncDecodeSuccess",
    "AsyncResponseHandler",
    "AsyncServiceCore",
    "AuthDescriptor",
    "AuthRegistry",
    "AuthRequirement",
    "AuthResolutionError",
    "AuthScheme",
    "Operation",
    "OperationInput",
    "ServiceCore",
    "ServiceDefaults",
    "StatusErrorMap",
    "assemble",
    "merge_async_config",
    "merge_config",
]
