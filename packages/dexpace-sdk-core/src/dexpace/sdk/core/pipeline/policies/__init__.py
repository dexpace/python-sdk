# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Built-in pipeline policies (sync + async)."""

from __future__ import annotations

from ._history import RequestHistory
from .async_client_identity import AsyncClientIdentityPolicy
from .async_idempotency import AsyncIdempotencyPolicy
from .async_redirect import AsyncRedirectPolicy
from .async_retry import AsyncRetryPolicy
from .async_set_date import AsyncSetDatePolicy
from .client_identity import ClientIdentityPolicy, default_user_agent
from .idempotency import IdempotencyPolicy
from .logging_policy import LoggingPolicy
from .redirect import RedirectPolicy
from .retry import RetryMode, RetryPolicy
from .set_date import SetDatePolicy
from .tracing_policy import TracingPolicy

__all__ = [
    "AsyncClientIdentityPolicy",
    "AsyncIdempotencyPolicy",
    "AsyncRedirectPolicy",
    "AsyncRetryPolicy",
    "AsyncSetDatePolicy",
    "ClientIdentityPolicy",
    "IdempotencyPolicy",
    "LoggingPolicy",
    "RedirectPolicy",
    "RequestHistory",
    "RetryMode",
    "RetryPolicy",
    "SetDatePolicy",
    "TracingPolicy",
    "default_user_agent",
]
