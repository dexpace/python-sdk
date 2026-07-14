# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncLoggableResponseBody`` (async twin of the sync capture)."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.response import AsyncLoggableResponseBody, AsyncResponseBody


async def _collect(body: AsyncResponseBody, chunk_size: int = 64 * 1024) -> bytes:
    chunks = [chunk async for chunk in body.aiter_bytes(chunk_size)]
    return b"".join(chunks)


class TestAsyncLoggableResponseBody:
    async def test_snapshot_returns_full_payload(self) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"hello world"))
        assert await wrapped.snapshot() == b"hello world"

    async def test_iter_bytes_repeatable_after_snapshot(self) -> None:
        # Fits-cap: the whole body is captured, so reads are fully repeatable.
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"abc"))
        _ = await wrapped.snapshot()
        assert await _collect(wrapped) == b"abc"
        assert await _collect(wrapped) == b"abc"

    async def test_snapshot_bounded_by_max_bytes(self) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"abcdefghij"))
        assert await wrapped.snapshot(4) == b"abcd"

    async def test_exceeds_cap_streams_full_body_but_caps_snapshot(self) -> None:
        # Exceeds-cap: snapshot keeps only the prefix, yet the reader still gets
        # every byte (prefix + streamed tail).
        wrapped = AsyncLoggableResponseBody(
            AsyncResponseBody.from_bytes(b"abcdefghij"), max_capture_bytes=4
        )
        assert await wrapped.snapshot() == b"abcd"
        assert await _collect(wrapped) == b"abcdefghij"

    async def test_exceeds_cap_tail_is_single_use(self) -> None:
        wrapped = AsyncLoggableResponseBody(
            AsyncResponseBody.from_bytes(b"abcdefghij"), max_capture_bytes=4
        )
        assert await _collect(wrapped) == b"abcdefghij"
        with pytest.raises(RuntimeError, match="single-use"):
            await _collect(wrapped)

    async def test_invalid_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b""), max_capture_bytes=0)

    async def test_close_is_idempotent(self) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"x"))
        await wrapped.close()
        await wrapped.close()

    async def test_snapshot_negative_max_bytes_raises(self) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"x"))
        with pytest.raises(ValueError, match="non-negative"):
            await wrapped.snapshot(-1)

    @pytest.mark.parametrize("size", [0, -1])
    async def test_aiter_bytes_rejects_invalid_chunk_size(self, size: int) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"abcdef"))
        with pytest.raises(ValueError, match="chunk_size"):
            await _collect(wrapped, size)

    async def test_content_length_reports_captured_size_when_fully_captured(self) -> None:
        wrapped = AsyncLoggableResponseBody(AsyncResponseBody.from_bytes(b"abcdef"))
        _ = await wrapped.snapshot()
        assert wrapped.content_length() == 6
