# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``ResponseBody`` factories and ``LoggableResponseBody``."""

from __future__ import annotations

from collections.abc import Callable
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

    @pytest.mark.parametrize("size", [0, -1])
    def test_iter_bytes_rejects_invalid_chunk_size(self, size: int) -> None:
        # A negative chunk_size used to make range(0, n, size) empty so the
        # body silently yielded nothing; it must raise ValueError like every
        # sibling body instead.
        wrapped = LoggableResponseBody(ResponseBody.from_bytes(b"abcdef"))
        with pytest.raises(ValueError, match="chunk_size"):
            list(wrapped.iter_bytes(size))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ResponseBody.from_bytes(b"hi"),
        lambda: ResponseBody.from_stream(BytesIO(b"hi")),
    ],
)
@pytest.mark.parametrize("size", [0, -1])
def test_iter_bytes_rejects_invalid_chunk_size(
    factory: Callable[[], ResponseBody],
    size: int,
) -> None:
    body = factory()
    with pytest.raises(ValueError, match="chunk_size"):
        list(body.iter_bytes(size))
