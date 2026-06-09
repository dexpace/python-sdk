# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for Standard Webhooks signature verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

import pytest

from dexpace.sdk.core.http.webhooks import (
    DEFAULT_TOLERANCE_SECONDS,
    InvalidWebhookSignatureError,
    WebhookVerifier,
)

# A known whsec_ secret and its raw base64 body. The key is "supersecret"
# base64-encoded so the test vectors are reproducible by hand if needed.
_RAW_KEY = b"supersecret-hmac-key-bytes-0123"
_SECRET = "whsec_" + base64.b64encode(_RAW_KEY).decode("ascii")

_WEBHOOK_ID = "msg_2KWPBgLlAfxdpx2AI54pPJ85f4W"
_TIMESTAMP = "1690000000"
_BODY = b'{"event":"payment.succeeded","amount":4200}'


class _FixedClock:
    """Minimal stationary clock pinned to a single wall-clock instant."""

    __slots__ = ("_t",)

    def __init__(self, t: float) -> None:
        self._t = t

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    def sleep(self, duration: float) -> None:  # pragma: no cover - unused
        raise AssertionError("verification must not sleep")


def _sign(key: bytes, webhook_id: str, timestamp: str, body: bytes) -> str:
    content = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, content, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _headers(signature: str, *, timestamp: str = _TIMESTAMP) -> dict[str, str]:
    return {
        "webhook-id": _WEBHOOK_ID,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


def _verifier(
    *,
    t: float = 1690000000.0,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> WebhookVerifier:
    return WebhookVerifier(_SECRET, tolerance_seconds=tolerance, clock=_FixedClock(t))


def test_verify_accepts_a_valid_signature() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    _verifier().verify(_headers(signature), _BODY)


def test_verify_accepts_a_string_body() -> None:
    body_str = _BODY.decode()
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    _verifier().verify(_headers(signature), body_str)


def test_verify_is_case_insensitive_for_header_names() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = {
        "Webhook-Id": _WEBHOOK_ID,
        "Webhook-Timestamp": _TIMESTAMP,
        "Webhook-Signature": f"v1,{signature}",
    }
    _verifier().verify(headers, _BODY)


def test_verify_rejects_a_tampered_body() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    tampered = _BODY.replace(b"4200", b"9999")
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _verifier().verify(_headers(signature), tampered)


def test_verify_rejects_a_tampered_webhook_id() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = _headers(signature)
    headers["webhook-id"] = "msg_attacker_swapped_this"
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _verifier().verify(headers, _BODY)


def test_verify_rejects_signature_made_with_a_different_secret() -> None:
    signature = _sign(b"a-completely-different-key", _WEBHOOK_ID, _TIMESTAMP, _BODY)
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _verifier().verify(_headers(signature), _BODY)


def test_verify_accepts_when_one_of_several_signatures_matches() -> None:
    good = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    bad = _sign(b"rotated-out-old-key", _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = {
        "webhook-id": _WEBHOOK_ID,
        "webhook-timestamp": _TIMESTAMP,
        "webhook-signature": f"v1,{bad} v1,{good}",
    }
    _verifier().verify(headers, _BODY)


def test_verify_rejects_when_no_signature_in_the_set_matches() -> None:
    bad1 = _sign(b"old-key-1", _WEBHOOK_ID, _TIMESTAMP, _BODY)
    bad2 = _sign(b"old-key-2", _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = {
        "webhook-id": _WEBHOOK_ID,
        "webhook-timestamp": _TIMESTAMP,
        "webhook-signature": f"v1,{bad1} v1,{bad2}",
    }
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _verifier().verify(headers, _BODY)


def test_verify_skips_unknown_version_tokens_and_still_matches_v1() -> None:
    good = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = {
        "webhook-id": _WEBHOOK_ID,
        "webhook-timestamp": _TIMESTAMP,
        "webhook-signature": f"v2,futurescheme v1,{good}",
    }
    _verifier().verify(headers, _BODY)


@pytest.mark.parametrize(
    "header",
    ["webhook-id", "webhook-timestamp", "webhook-signature"],
    ids=["missing_id", "missing_timestamp", "missing_signature"],
)
def test_verify_rejects_when_a_required_header_is_missing(header: str) -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = _headers(signature)
    del headers[header]
    with pytest.raises(InvalidWebhookSignatureError, match=f"missing required header: {header}"):
        _verifier().verify(headers, _BODY)


def test_verify_rejects_a_timestamp_older_than_tolerance() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    # Now is just past the tolerance window after the signed instant.
    now = int(_TIMESTAMP) + DEFAULT_TOLERANCE_SECONDS + 1
    with pytest.raises(InvalidWebhookSignatureError, match="too old"):
        _verifier(t=now).verify(_headers(signature), _BODY)


def test_verify_rejects_a_timestamp_too_far_in_the_future() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    now = int(_TIMESTAMP) - DEFAULT_TOLERANCE_SECONDS - 1
    with pytest.raises(InvalidWebhookSignatureError, match="in the future"):
        _verifier(t=now).verify(_headers(signature), _BODY)


def test_verify_accepts_a_timestamp_at_the_edge_of_the_window() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    now = int(_TIMESTAMP) + DEFAULT_TOLERANCE_SECONDS
    _verifier(t=now).verify(_headers(signature), _BODY)


def test_verify_rejects_a_non_integer_timestamp() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers = _headers(signature, timestamp="not-a-number")
    with pytest.raises(InvalidWebhookSignatureError, match="malformed webhook-timestamp"):
        _verifier().verify(headers, _BODY)


def test_whsec_prefix_is_stripped_and_base64_decoded() -> None:
    # A verifier built from the whsec_-prefixed secret produces the same result
    # as signing with the raw decoded key — i.e. the prefix was stripped and
    # the body base64-decoded to recover exactly _RAW_KEY.
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    _verifier().verify(_headers(signature), _BODY)


def test_secret_without_whsec_prefix_is_accepted() -> None:
    raw_b64 = base64.b64encode(_RAW_KEY).decode("ascii")
    verifier = WebhookVerifier(raw_b64, clock=_FixedClock(1690000000.0))
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    verifier.verify(_headers(signature), _BODY)


def test_malformed_secret_raises_at_construction() -> None:
    with pytest.raises(InvalidWebhookSignatureError, match="malformed webhook secret"):
        WebhookVerifier("whsec_not!valid!base64!")


def test_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        WebhookVerifier(_SECRET, tolerance_seconds=-1)


def test_unwrap_returns_parsed_json_on_success() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    payload = _verifier().unwrap(_headers(signature), _BODY)
    assert payload == {"event": "payment.succeeded", "amount": 4200}


def test_unwrap_parses_the_exact_verified_bytes() -> None:
    body = json.dumps({"nested": {"k": [1, 2, 3]}, "flag": True}).encode()
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, body)
    payload = _verifier().unwrap(_headers(signature), body)
    assert payload == {"nested": {"k": [1, 2, 3]}, "flag": True}


def test_unwrap_does_not_parse_an_unverified_body() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    tampered = b'{"event":"tampered"}'
    with pytest.raises(InvalidWebhookSignatureError, match="no matching signature"):
        _verifier().unwrap(_headers(signature), tampered)


def test_unwrap_rejects_a_verified_but_non_json_body() -> None:
    body = b"this is signed but is not json"
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, body)
    with pytest.raises(InvalidWebhookSignatureError, match="not valid JSON"):
        _verifier().unwrap(_headers(signature), body)


def test_invalid_signature_error_is_a_value_error() -> None:
    assert issubclass(InvalidWebhookSignatureError, ValueError)


def test_verifier_accepts_a_generic_mapping() -> None:
    signature = _sign(_RAW_KEY, _WEBHOOK_ID, _TIMESTAMP, _BODY)
    headers: Mapping[str, str] = _headers(signature)
    _verifier().verify(headers, _BODY)
