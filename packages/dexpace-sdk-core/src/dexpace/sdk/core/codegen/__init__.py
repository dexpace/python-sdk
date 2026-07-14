# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tier-1 declarative request-assembly contract.

The `Operation` / `OperationInput` value objects describe a request the way a
generated SDK client would, and `assemble` turns that declaration into a
concrete `Request` (method, URL, headers). Body serialization is intentionally
left to a later executor layer.
"""

from __future__ import annotations

from .assembly import assemble
from .operation import Operation, OperationInput

__all__ = ["Operation", "OperationInput", "assemble"]
