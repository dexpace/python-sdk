# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed exception hierarchy for the SDK.

Modelled on Azure SDK for Python's ``corehttp.exceptions`` but slimmed to the
classes the SDK actually raises. The hierarchy has two branches, split by
whether a response was ever received:

- ``NetworkError`` — the transport branch. A transport-level failure that never
  yielded a usable response (connection refused, DNS failure, connect timeout).
  Subclasses ``OSError`` so ``except OSError:`` sites keep matching it, and is
  always retryable at the error-class level. ``ServiceRequestError`` (and its
  ``ServiceRequestTimeoutError``) live here.
- ``HttpResponseError`` — the response branch. A 4xx or 5xx response was
  received intact; retryability is derived per-status from the shared
  classifier and baked once at construction. Carries a bounded, replayable copy
  of the error body (``read_body``, capped at ``ERROR_BODY_MAX_BYTES``).

``ServiceResponseError`` is the in-between case (request sent, but the response
could not be parsed): it is not on the always-retryable transport branch
because the request may already have been processed.

``RequestCancelledError`` is the terminal type for synchronous cooperative
cancellation; like ``NetworkError`` it is an ``OSError`` but, being terminal, it
is not a ``NetworkError`` and is never retryable.

Plus exceptions for body / stream lifecycle violations
(``StreamConsumedError``, ``StreamClosedError``, ``ResponseNotReadError``,
``StreamingError``), serde failures (``SerdeError`` and its directional
``SerializationError`` / ``DeserializationError`` subtypes), and pipeline
aborts (``PipelineAbortedError``).
"""

from __future__ import annotations

from .base import (
    NetworkError,
    RequestCancelledError,
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from .error_map import map_error
from .http import (
    ERROR_BODY_MAX_BYTES,
    ClientAuthenticationError,
    DecodeError,
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    ResourceNotModifiedError,
)
from .pipeline import PipelineAbortedError
from .serialization import DeserializationError, SerdeError, SerializationError
from .streaming import (
    ResponseNotReadError,
    StreamClosedError,
    StreamConsumedError,
    StreamingError,
)

__all__ = [
    "ERROR_BODY_MAX_BYTES",
    "ClientAuthenticationError",
    "DecodeError",
    "DeserializationError",
    "HttpResponseError",
    "NetworkError",
    "PipelineAbortedError",
    "RequestCancelledError",
    "ResourceExistsError",
    "ResourceModifiedError",
    "ResourceNotFoundError",
    "ResourceNotModifiedError",
    "ResponseNotReadError",
    "SdkError",
    "SerdeError",
    "SerializationError",
    "ServiceRequestError",
    "ServiceRequestTimeoutError",
    "ServiceResponseError",
    "ServiceResponseTimeoutError",
    "StreamClosedError",
    "StreamConsumedError",
    "StreamingError",
    "map_error",
]
