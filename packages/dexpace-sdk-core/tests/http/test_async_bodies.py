# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncRequestBody`` / ``AsyncResponseBody``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest

from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.http.request import AsyncRequestBody
from dexpace.sdk.core.http.response import AsyncResponseBody


async def _adrain(body: AsyncRequestBody) -> bytes:
    chunks: list[bytes] = []
    async for chunk in body.aiter_bytes():
        chunks.append(chunk)
    return b"".join(chunks)


async def test_from_bytes_replayable() -> None:
    body = AsyncRequestBody.from_bytes(b"hello")
    assert body.is_replayable()
    assert await _adrain(body) == b"hello"
    assert await _adrain(body) == b"hello"


async def test_from_string() -> None:
    body = AsyncRequestBody.from_string("hello")
    assert await _adrain(body) == b"hello"


async def test_from_form() -> None:
    body = AsyncRequestBody.from_form({"a": "1"})
    text = (await _adrain(body)).decode()
    assert text == "a=1"
    assert body.media_type() == common_media_types.APPLICATION_FORM_URLENCODED


async def test_from_form_encoding_changes_percent_encoding() -> None:
    # Async twin of the sync charset test: a non-ASCII field must percent-encode
    # through the requested charset, so latin-1 and utf-8 differ byte-for-byte.
    fields = {"name": "é"}
    latin1 = await _adrain(AsyncRequestBody.from_form(fields, encoding="latin-1"))
    utf8 = await _adrain(AsyncRequestBody.from_form(fields, encoding="utf-8"))
    assert latin1 == b"name=%E9"
    assert utf8 == b"name=%C3%A9"
    assert latin1 != utf8


async def test_from_async_iter_single_use() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"c"

    body = AsyncRequestBody.from_async_iter(chunks())
    assert await _adrain(body) == b"abc"
    with pytest.raises(RuntimeError):
        await _adrain(body)


async def test_to_replayable_buffers_async_iter() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"c"

    body = await AsyncRequestBody.from_async_iter(chunks()).to_replayable()
    assert body.is_replayable()
    assert await _adrain(body) == b"abc"
    assert await _adrain(body) == b"abc"


async def test_async_response_body_bytes() -> None:
    body = AsyncResponseBody.from_bytes(b"hello")
    assert await body.bytes() == b"hello"


async def test_async_response_body_string() -> None:
    body = AsyncResponseBody.from_bytes("héllo".encode())
    assert await body.string() == "héllo"


async def test_async_response_body_chunks() -> None:
    body = AsyncResponseBody.from_bytes(b"abcdef")
    chunks = [chunk async for chunk in body.aiter_bytes(chunk_size=2)]
    assert chunks == [b"ab", b"cd", b"ef"]


async def test_async_context_manager_closes() -> None:
    body = AsyncResponseBody.from_bytes(b"x")
    async with body as b:
        assert b is body


class _StubAsyncStream:
    """Minimal ``SupportsAsyncRead`` stub for the chunk-size guard test."""

    async def read(self, size: int = -1) -> bytes:
        del size
        return b""

    async def close(self) -> object:  # pragma: no cover - never reached
        return None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AsyncResponseBody.from_bytes(b"hi"),
        lambda: AsyncResponseBody.from_async_stream(_StubAsyncStream()),
    ],
)
@pytest.mark.parametrize("size", [0, -1])
async def test_aiter_bytes_rejects_invalid_chunk_size(
    factory: Callable[[], AsyncResponseBody],
    size: int,
) -> None:
    body = factory()
    with pytest.raises(ValueError, match="chunk_size"):
        async for _ in body.aiter_bytes(size):
            pass


async def _one_chunk() -> AsyncIterator[bytes]:
    yield b"hi"


def _make_async_request_bodies() -> list[Callable[[], AsyncRequestBody]]:
    return [
        lambda: AsyncRequestBody.from_bytes(b"hi"),
        lambda: AsyncRequestBody.from_async_iter(_one_chunk()),
        lambda: AsyncRequestBody.from_async_stream(_StubAsyncStream()),
    ]


@pytest.mark.parametrize("factory", _make_async_request_bodies())
@pytest.mark.parametrize("size", [0, -1])
async def test_async_request_aiter_bytes_rejects_invalid_chunk_size(
    factory: Callable[[], AsyncRequestBody],
    size: int,
) -> None:
    # All three AsyncRequestBody backings must reject a non-positive chunk_size
    # up front, matching the sync and response-body guard.
    body = factory()
    with pytest.raises(ValueError, match="chunk_size"):
        async for _ in body.aiter_bytes(size):
            pass
