# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tier-1 declarative request-assembly contract.

The `Operation` / `OperationInput` value objects describe a request the way a
generated SDK client would, and `assemble` turns that declaration into a
concrete `Request` (method, URL, headers). Body serialization is intentionally
left to a later executor layer.

`StatusErrorMap` is the response-side companion: a frozen status-code →
typed-error mapping a generated client reads as data to turn an error response
into the right `HttpResponseError` subclass.
"""

from __future__ import annotations

from .assembly import assemble
from .operation import Operation, OperationInput
from .status_error_map import StatusErrorMap

__all__ = ["Operation", "OperationInput", "StatusErrorMap", "assemble"]
