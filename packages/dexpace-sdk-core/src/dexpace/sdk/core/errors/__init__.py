# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed exception hierarchy for the SDK.

Modelled on Azure SDK for Python's ``corehttp.exceptions`` but slimmed to the
classes the SDK actually raises. The hierarchy distinguishes three failure
shapes:

- ``ServiceRequestError`` — request never reached the server (DNS failure,
  connection refused, etc.). Safe to retry on idempotent methods.
- ``ServiceResponseError`` — request was sent but the response could not be
  parsed (connection drop mid-response, decode failure on a chunked stream).
- ``HttpResponseError`` — a 4xx or 5xx response was received intact. Carries
  the response so callers can inspect status, headers, and body.

Plus exceptions for body / stream lifecycle violations
(``StreamConsumedError``, ``StreamClosedError``, ``ResponseNotReadError``,
``StreamingError``), serialization (``SerializationError``,
``DeserializationError``), and pipeline aborts (``PipelineAbortedError``).
"""

from __future__ import annotations

from .base import (
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from .error_map import map_error
from .http import (
    ClientAuthenticationError,
    DecodeError,
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    ResourceNotModifiedError,
)
from .pipeline import PipelineAbortedError
from .serialization import DeserializationError, SerializationError
from .streaming import (
    ResponseNotReadError,
    StreamClosedError,
    StreamConsumedError,
    StreamingError,
)

__all__ = [
    "ClientAuthenticationError",
    "DecodeError",
    "DeserializationError",
    "HttpResponseError",
    "PipelineAbortedError",
    "ResourceExistsError",
    "ResourceModifiedError",
    "ResourceNotFoundError",
    "ResourceNotModifiedError",
    "ResponseNotReadError",
    "SdkError",
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
