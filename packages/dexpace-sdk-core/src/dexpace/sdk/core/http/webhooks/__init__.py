# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Standard Webhooks signature verification (standardwebhooks.com).

Stdlib-only HMAC-SHA256 verification of inbound webhooks: rebuild the signed
content ``{id}.{timestamp}.{body}``, compare constant-time against any of the
provided ``v1,<base64>`` signatures, and reject deliveries outside a ±5-minute
timestamp window. `WebhookVerifier.unwrap` additionally parses the
verified JSON body.
"""

from __future__ import annotations

from .verification import (
    DEFAULT_TOLERANCE_SECONDS,
    InvalidWebhookSignatureError,
    WebhookVerifier,
)

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "InvalidWebhookSignatureError",
    "WebhookVerifier",
]
