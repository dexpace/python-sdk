# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Standard Webhooks signature verification.

Implements the `Standard Webhooks <https://www.standardwebhooks.com>`_ scheme
for verifying inbound webhook payloads, using the standard library only.

A producer signs each webhook by computing ``HMAC-SHA256`` over the content
``{id}.{timestamp}.{body}`` keyed by a shared secret, base64-encoding the
digest, and prefixing it with the scheme version (``v1,``). The three pieces a
receiver needs travel in headers:

- ``webhook-id``: an opaque message identifier, also part of the signed content
  so a signature cannot be replayed against a different message id.
- ``webhook-timestamp``: the Unix epoch second the message was signed, used to
  reject stale deliveries outside a tolerance window.
- ``webhook-signature``: one or more space-separated ``v1,<base64>`` tokens. A
  producer may publish several (e.g. during secret rotation); a match against
  any one is sufficient.

The shared secret is supplied in its on-the-wire form, ``whsec_<base64>``; the
prefix is stripped and the remainder base64-decoded to recover the raw HMAC
key.

Verification is constant-time (``hmac.compare_digest``) and rejects timestamps
that are too old or too far in the future relative to an injected
:class:`~dexpace.sdk.core.util.Clock`, defaulting to the process clock.

Example:
    >>> verifier = WebhookVerifier("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
    >>> headers = {
    ...     "webhook-id": "msg_2KWPBgLlAfxdpx2AI54pPJ85f4W",
    ...     "webhook-timestamp": "1690000000",
    ...     "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
    ... }
    >>> payload = verifier.unwrap(headers, request_body)  # parsed JSON dict
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Final

from dexpace.sdk.core.util import SYSTEM_CLOCK, Clock

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "InvalidWebhookSignatureError",
    "WebhookVerifier",
]

_SECRET_PREFIX: Final[str] = "whsec_"
_SIGNATURE_VERSION: Final[str] = "v1"
_ID_HEADER: Final[str] = "webhook-id"
_TIMESTAMP_HEADER: Final[str] = "webhook-timestamp"
_SIGNATURE_HEADER: Final[str] = "webhook-signature"

DEFAULT_TOLERANCE_SECONDS: Final[int] = 5 * 60
"""Default ``±`` timestamp tolerance in seconds (5 minutes per the spec)."""


class InvalidWebhookSignatureError(ValueError):
    """Raised when a webhook payload fails verification.

    A ``ValueError`` subclass so callers can catch it as either the specific
    type or the broad input-validation category. The message never echoes the
    secret or the expected signature, only the reason the check failed.
    """


def _require_header(headers: Mapping[str, str], name: str) -> str:
    """Return the value of ``name`` from ``headers`` (case-insensitive).

    Args:
        headers: Inbound request headers.
        name: Lower-case header name to look up.

    Returns:
        The header value.

    Raises:
        InvalidWebhookSignatureError: If the header is missing.
    """
    value = headers.get(name)
    if value is None:
        lowered = {key.lower(): val for key, val in headers.items()}
        value = lowered.get(name)
    if value is None:
        raise InvalidWebhookSignatureError(f"missing required header: {name}")
    return value


def _decode_secret(secret: str) -> bytes:
    """Decode a ``whsec_``-prefixed base64 secret into the raw HMAC key.

    Args:
        secret: The shared secret, with or without the ``whsec_`` prefix. The
            prefix is optional so a caller that already stripped it still works.

    Returns:
        The raw key bytes.

    Raises:
        InvalidWebhookSignatureError: If the base64 body is malformed.
    """
    body = secret[len(_SECRET_PREFIX) :] if secret.startswith(_SECRET_PREFIX) else secret
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidWebhookSignatureError("malformed webhook secret") from exc


def _as_bytes(body: str | bytes) -> bytes:
    """Return ``body`` as UTF-8 bytes, passing through ``bytes`` unchanged."""
    return body if isinstance(body, bytes) else body.encode("utf-8")


class WebhookVerifier:
    """Verifies inbound webhooks against the Standard Webhooks scheme.

    Immutable after construction: the decoded key, tolerance, and clock are
    fixed. Safe to share across threads — verification holds no mutable state.

    Args:
        secret: The shared signing secret in ``whsec_<base64>`` form (the
            prefix is optional).
        tolerance_seconds: Maximum absolute difference, in seconds, allowed
            between the signed timestamp and the current time. Defaults to
            :data:`DEFAULT_TOLERANCE_SECONDS` (5 minutes).
        clock: Time source used to evaluate the tolerance window. Defaults to
            the process clock; inject a fake to test the replay window.

    Raises:
        InvalidWebhookSignatureError: If ``secret`` is malformed.
        ValueError: If ``tolerance_seconds`` is negative.
    """

    __slots__ = ("_clock", "_key", "_tolerance_seconds")

    def __init__(
        self,
        secret: str,
        *,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if tolerance_seconds < 0:
            raise ValueError(f"tolerance_seconds must be non-negative, got {tolerance_seconds}")
        self._key: Final[bytes] = _decode_secret(secret)
        self._tolerance_seconds: Final[int] = tolerance_seconds
        self._clock: Final[Clock] = clock

    def verify(self, headers: Mapping[str, str], body: str | bytes) -> None:
        """Verify a webhook delivery, raising on any failure.

        Looks up the ``webhook-id`` / ``webhook-timestamp`` / ``webhook-
        signature`` headers (case-insensitively), checks the timestamp against
        the tolerance window, recomputes the expected signature over
        ``{id}.{timestamp}.{body}``, and compares it constant-time against each
        provided signature token. Returns normally when at least one token
        matches.

        Args:
            headers: Inbound request headers. Lookups are case-insensitive.
            body: The raw request body, exactly as received. Passing a
                re-serialized form risks a byte mismatch and a spurious
                failure, so prefer the original bytes.

        Raises:
            InvalidWebhookSignatureError: If a required header is missing, the
                timestamp is malformed or outside the tolerance window, or no
                provided signature matches.
        """
        webhook_id = _require_header(headers, _ID_HEADER)
        timestamp = _require_header(headers, _TIMESTAMP_HEADER)
        signature_header = _require_header(headers, _SIGNATURE_HEADER)

        self._check_timestamp(timestamp)
        expected = self._sign(webhook_id, timestamp, _as_bytes(body))
        if not self._matches_any(signature_header, expected):
            raise InvalidWebhookSignatureError("no matching signature")

    def unwrap(self, headers: Mapping[str, str], body: str | bytes) -> object:
        """Verify a webhook and return its parsed JSON payload.

        A convenience over :meth:`verify` for the common case of a JSON body:
        the signature is checked against the *raw* bytes first, then the same
        bytes are parsed. Verifying before parsing guarantees only authentic
        payloads are ever deserialized.

        Args:
            headers: Inbound request headers.
            body: The raw request body, exactly as received.

        Returns:
            The decoded JSON value (typically a ``dict`` for an object body,
            but any JSON value is returned as-is).

        Raises:
            InvalidWebhookSignatureError: If verification fails or the verified
                body is not valid JSON.
        """
        raw = _as_bytes(body)
        self.verify(headers, raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidWebhookSignatureError("verified body is not valid JSON") from exc

    def _check_timestamp(self, timestamp: str) -> None:
        """Reject a timestamp that is malformed or outside the tolerance window.

        Raises:
            InvalidWebhookSignatureError: If ``timestamp`` is not an integer or
                differs from now by more than the configured tolerance.
        """
        try:
            signed_at = int(timestamp)
        except ValueError as exc:
            raise InvalidWebhookSignatureError("malformed webhook-timestamp") from exc
        delta = self._clock.now() - signed_at
        if delta > self._tolerance_seconds:
            raise InvalidWebhookSignatureError("webhook timestamp is too old")
        if delta < -self._tolerance_seconds:
            raise InvalidWebhookSignatureError("webhook timestamp is in the future")

    def _sign(self, webhook_id: str, timestamp: str, body: bytes) -> str:
        """Return the base64 ``HMAC-SHA256`` over ``{id}.{timestamp}.{body}``."""
        signed_content = b"%s.%s." % (webhook_id.encode("utf-8"), timestamp.encode("utf-8"))
        digest = hmac.new(self._key, signed_content + body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _matches_any(self, signature_header: str, expected: str) -> bool:
        """Return whether any ``v1,<base64>`` token equals ``expected``.

        Tokens are space-separated. Unknown-version and malformed tokens are
        skipped rather than raising, so a forward-compatible producer that adds
        a future scheme version alongside ``v1`` still verifies. Comparison is
        constant-time to avoid leaking the expected signature via timing.
        """
        expected_bytes = expected.encode("ascii")
        matched = False
        for token in signature_header.split(" "):
            version, _, candidate = token.partition(",")
            if version != _SIGNATURE_VERSION or not candidate:
                continue
            if hmac.compare_digest(candidate.encode("ascii"), expected_bytes):
                matched = True
        return matched
