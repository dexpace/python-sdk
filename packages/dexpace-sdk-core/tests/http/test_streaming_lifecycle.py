# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Lifecycle discipline for the jsonl / chunked-frame streaming iterators.

Mirrors the discipline the SSE parser already enforces: the decoders wrap an
underlying byte stream and must eagerly release it — on clean exhaustion, on a
mid-iteration error, and when the consumer abandons iteration part-way — while
never letting a teardown-time close error mask the primary error that ended
iteration.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Iterable
from typing import Any, cast

import pytest

from dexpace.sdk.core.errors import DeserializationError
from dexpace.sdk.core.http.common import (
    aiter_chunked_frame,
    aiter_jsonl,
    chunked_frame,
    iter_jsonl,
)


class _ClosableChunks:
    """Sync byte source exposing an explicit ``close`` the decoder must call.

    Unlike a generator (whose ``finally`` runs on exhaustion), a plain iterator
    is only released when its ``close`` is invoked, so it pins down that the
    decoder itself performs the release.
    """

    def __init__(self, chunks: Iterable[bytes], *, fail_close: bool = False) -> None:
        self._it = iter(chunks)
        self.closed = False
        self._fail_close = fail_close

    def __iter__(self) -> _ClosableChunks:
        return self

    def __next__(self) -> bytes:
        return next(self._it)

    def close(self) -> None:
        self.closed = True
        if self._fail_close:
            raise RuntimeError("upstream close failed")


class _AsyncClosableChunks:
    """Async twin of `_ClosableChunks`."""

    def __init__(self, chunks: Iterable[bytes], *, fail_close: bool = False) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self.closed = False
        self._fail_close = fail_close

    def __aiter__(self) -> _AsyncClosableChunks:
        return self

    async def __anext__(self) -> bytes:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True
        if self._fail_close:
            raise RuntimeError("upstream aclose failed")


def _context_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        chain.append(node)
        seen.add(id(node))
        node = node.__context__
    return chain


class TestIterJsonlLifecycle:
    def test_closes_upstream_on_exhaustion(self) -> None:
        source = _ClosableChunks([b'{"a": 1}\n{"b": 2}\n'])
        assert list(iter_jsonl(source)) == [{"a": 1}, {"b": 2}]
        assert source.closed is True

    def test_closes_upstream_when_abandoned_mid_iteration(self) -> None:
        source = _ClosableChunks([b'{"a": 1}\n{"b": 2}\n{"c": 3}\n'])
        stream = cast(Generator[Any, None, None], iter_jsonl(source))
        assert next(stream) == {"a": 1}
        stream.close()
        assert source.closed is True

    def test_closes_upstream_on_parse_error(self) -> None:
        source = _ClosableChunks([b"{not json}\n"])
        with pytest.raises(DeserializationError):
            list(iter_jsonl(source))
        assert source.closed is True

    def test_close_error_does_not_mask_primary_error(self) -> None:
        source = _ClosableChunks([b"{not json}\n"], fail_close=True)
        with pytest.raises(DeserializationError) as exc_info:
            list(iter_jsonl(source))
        # The parse error wins; the close failure is preserved on the chain.
        chain = _context_chain(exc_info.value)
        assert any(isinstance(node, RuntimeError) for node in chain)

    def test_close_error_surfaces_when_no_primary_error(self) -> None:
        source = _ClosableChunks([b'{"a": 1}\n'], fail_close=True)
        with pytest.raises(RuntimeError, match="upstream close failed"):
            list(iter_jsonl(source))


class TestChunkedFrameLifecycle:
    def test_closes_upstream_on_exhaustion(self) -> None:
        source = _ClosableChunks([b"hello", b"world"])
        list(chunked_frame(source))
        assert source.closed is True

    def test_closes_upstream_when_abandoned_mid_iteration(self) -> None:
        source = _ClosableChunks([b"hello", b"world"])
        stream = cast(Generator[bytes, None, None], chunked_frame(source))
        next(stream)
        stream.close()
        assert source.closed is True


class TestAsyncIterJsonlLifecycle:
    async def test_closes_upstream_on_exhaustion(self) -> None:
        source = _AsyncClosableChunks([b'{"a": 1}\n{"b": 2}\n'])
        result = [value async for value in aiter_jsonl(source)]
        assert result == [{"a": 1}, {"b": 2}]
        assert source.closed is True

    async def test_closes_upstream_when_abandoned_mid_iteration(self) -> None:
        source = _AsyncClosableChunks([b'{"a": 1}\n{"b": 2}\n{"c": 3}\n'])
        stream = cast(AsyncGenerator[Any, None], aiter_jsonl(source))
        assert await stream.__anext__() == {"a": 1}
        await stream.aclose()
        assert source.closed is True

    async def test_closes_upstream_on_parse_error(self) -> None:
        source = _AsyncClosableChunks([b"{not json}\n"])
        with pytest.raises(DeserializationError):
            async for _ in aiter_jsonl(source):
                pass
        assert source.closed is True

    async def test_close_error_does_not_mask_primary_error(self) -> None:
        source = _AsyncClosableChunks([b"{not json}\n"], fail_close=True)
        with pytest.raises(DeserializationError) as exc_info:
            async for _ in aiter_jsonl(source):
                pass
        chain = _context_chain(exc_info.value)
        assert any(isinstance(node, RuntimeError) for node in chain)


class TestAsyncChunkedFrameLifecycle:
    async def test_closes_upstream_on_exhaustion(self) -> None:
        source = _AsyncClosableChunks([b"hello", b"world"])
        _ = [piece async for piece in aiter_chunked_frame(source)]
        assert source.closed is True

    async def test_closes_upstream_when_abandoned_mid_iteration(self) -> None:
        source = _AsyncClosableChunks([b"hello", b"world"])
        stream = cast(AsyncGenerator[bytes, None], aiter_chunked_frame(source))
        await stream.__anext__()
        await stream.aclose()
        assert source.closed is True
