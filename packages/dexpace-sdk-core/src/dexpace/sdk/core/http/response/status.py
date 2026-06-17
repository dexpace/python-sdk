# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Canonical HTTP status codes."""

from __future__ import annotations

from enum import IntEnum


class Status(IntEnum):
    """HTTP status codes recognized by the SDK.

    Inheriting from `int` so callers can compare against integers and
    range-check directly: ``response.status == 200`` or ``200 <= status < 300``.

    Lookup is lenient: `Status(code)` for any integer in the HTTP range
    100..599 that is not a named member returns a synthesized pseudo-member
    named ``UNKNOWN_<code>`` carrying the raw integer value. This lets
    responses with unregistered-but-valid codes (for example ``218`` from
    Apache or ``599`` from a proxy) flow through the SDK with their band
    classification (`is_success`, `is_redirect`, ...) and integer comparisons
    intact, instead of being discarded. Integers outside 100..599 (for
    example ``42`` or ``1000``) remain invalid and raise `ValueError`.

    .. note::
        Synthesized ``UNKNOWN_<code>`` members are dynamically instantiated and 
        are not identity-stable across lookups. Always use value equality 
        (``==``) or band predicates rather than identity checks (``is``).
    """

    # 1xx Informational
    CONTINUE = 100
    SWITCHING_PROTOCOLS = 101
    PROCESSING = 102
    EARLY_HINTS = 103

    # 2xx Success
    OK = 200
    CREATED = 201
    ACCEPTED = 202
    NON_AUTHORITATIVE_INFORMATION = 203
    NO_CONTENT = 204
    RESET_CONTENT = 205
    PARTIAL_CONTENT = 206
    MULTI_STATUS = 207
    ALREADY_REPORTED = 208
    IM_USED = 226

    # 3xx Redirection
    MULTIPLE_CHOICES = 300
    MOVED_PERMANENTLY = 301
    FOUND = 302
    SEE_OTHER = 303
    NOT_MODIFIED = 304
    USE_PROXY = 305
    TEMPORARY_REDIRECT = 307
    PERMANENT_REDIRECT = 308

    # 4xx Client Error
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    PAYMENT_REQUIRED = 402
    FORBIDDEN = 403
    NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    NOT_ACCEPTABLE = 406
    PROXY_AUTHENTICATION_REQUIRED = 407
    REQUEST_TIMEOUT = 408
    CONFLICT = 409
    GONE = 410
    LENGTH_REQUIRED = 411
    PRECONDITION_FAILED = 412
    PAYLOAD_TOO_LARGE = 413
    URI_TOO_LONG = 414
    UNSUPPORTED_MEDIA_TYPE = 415
    RANGE_NOT_SATISFIABLE = 416
    EXPECTATION_FAILED = 417
    IM_A_TEAPOT = 418
    MISDIRECTED_REQUEST = 421
    UNPROCESSABLE_ENTITY = 422
    LOCKED = 423
    FAILED_DEPENDENCY = 424
    TOO_EARLY = 425
    UPGRADE_REQUIRED = 426
    PRECONDITION_REQUIRED = 428
    TOO_MANY_REQUESTS = 429
    REQUEST_HEADER_FIELDS_TOO_LARGE = 431
    UNAVAILABLE_FOR_LEGAL_REASONS = 451

    # 5xx Server Error
    INTERNAL_SERVER_ERROR = 500
    NOT_IMPLEMENTED = 501
    BAD_GATEWAY = 502
    SERVICE_UNAVAILABLE = 503
    GATEWAY_TIMEOUT = 504
    HTTP_VERSION_NOT_SUPPORTED = 505
    VARIANT_ALSO_NEGOTIATES = 506
    INSUFFICIENT_STORAGE = 507
    LOOP_DETECTED = 508
    NOT_EXTENDED = 510
    NETWORK_AUTHENTICATION_REQUIRED = 511

    @classmethod
    def _missing_(cls, value: object) -> Status | None:
        """Synthesize a pseudo-member for an unregistered valid HTTP code.

        Args:
            value: The lookup value passed to `Status(value)`.

        Returns:
            A pseudo-member carrying `value` when it is an integer in the
            HTTP range 100..599 with no named member, or `None` to let the
            enum machinery raise `ValueError` for any other input.
        """
        if isinstance(value, int) and 100 <= value <= 599:
            pseudo = int.__new__(cls, value)
            pseudo._name_ = f"UNKNOWN_{value}"
            pseudo._value_ = value
            return pseudo
        return None

    @property
    def is_informational(self) -> bool:
        return 100 <= self.value < 200

    @property
    def is_success(self) -> bool:
        return 200 <= self.value < 300

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.value < 400

    @property
    def is_client_error(self) -> bool:
        return 400 <= self.value < 500

    @property
    def is_server_error(self) -> bool:
        return 500 <= self.value < 600

    @property
    def is_error(self) -> bool:
        return self.is_client_error or self.is_server_error


__all__ = ["Status"]
