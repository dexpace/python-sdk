"""Tests for ``ResponseBody`` factories and ``LoggableResponseBody``."""
from __future__ import annotations

from io import BytesIO

import pytest

from dexpace.sdk.core.http.common import MediaType
from dexpace.sdk.core.http.response import LoggableResponseBody, ResponseBody


def test_from_bytes_returns_bytes() -> None:
    body = ResponseBody.from_bytes(b"hello")
    assert body.bytes() == b"hello"


def test_string_uses_media_type_charset() -> None:
    body = ResponseBody.from_bytes(
        "héllo".encode("latin-1"),
        media_type=MediaType.parse("text/plain; charset=latin-1"),
    )
    assert body.string() == "héllo"


def test_string_defaults_to_utf8() -> None:
    body = ResponseBody.from_bytes("héllo".encode())
    assert body.string() == "héllo"


def test_context_manager_closes() -> None:
    body = ResponseBody.from_bytes(b"x")
    with body as b:
        assert b is body


def test_single_use_after_bytes() -> None:
    body = ResponseBody.from_bytes(b"once")
    assert body.bytes() == b"once"
    with pytest.raises(RuntimeError):
        body.bytes()


def test_iter_bytes_in_chunks() -> None:
    body = ResponseBody.from_bytes(b"abcdef")
    chunks = list(body.iter_bytes(chunk_size=2))
    assert chunks == [b"ab", b"cd", b"ef"]


def test_from_stream() -> None:
    body = ResponseBody.from_stream(BytesIO(b"streamed"), content_length=8)
    assert body.bytes() == b"streamed"


class TestLoggableResponseBody:
    def test_snapshot_returns_full_payload(self) -> None:
        wrapped = LoggableResponseBody(ResponseBody.from_bytes(b"hello world"))
        assert wrapped.snapshot() == b"hello world"

    def test_iter_bytes_repeatable_after_snapshot(self) -> None:
        wrapped = LoggableResponseBody(ResponseBody.from_bytes(b"abc"))
        _ = wrapped.snapshot()
        assert b"".join(wrapped.iter_bytes()) == b"abc"
        assert b"".join(wrapped.iter_bytes()) == b"abc"

    def test_cap_applied_to_capture(self) -> None:
        wrapped = LoggableResponseBody(
            ResponseBody.from_bytes(b"abcdefghij"),
            max_capture_bytes=4,
        )
        snap = wrapped.snapshot()
        assert len(snap) <= 4

    def test_invalid_cap_raises(self) -> None:
        with pytest.raises(ValueError):
            LoggableResponseBody(ResponseBody.from_bytes(b""), max_capture_bytes=0)

    def test_close_is_idempotent(self) -> None:
        wrapped = LoggableResponseBody(ResponseBody.from_bytes(b"x"))
        wrapped.close()
        wrapped.close()
