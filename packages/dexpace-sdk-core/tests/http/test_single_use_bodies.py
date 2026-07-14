# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Certification battery for single-use body semantics + the lock-protected once-guard.

Covers, for the stream/iter-backed ``RequestBody`` / ``ResponseBody`` families
and their async twins:

* double-consume raises ``RuntimeError`` with a consistent, documented message;
* the "consumed" decision is an *eager*, lock-protected check-then-act — a
  second ``iter_bytes`` / ``aiter_bytes`` call raises at call time, before the
  returned iterator is advanced, so two undrained iterators can never both
  believe the body is fresh;
* N threads racing the very first consumption yield exactly one winner. The
  guard is a ``threading.Lock``, never a bare ``bool``, so this holds without
  relying on GIL atomicity of the flag read/write. (A free-threaded CPython CI
  row is future infra; the code is correct regardless of interpreter build.)
* ``to_replayable`` round-trips identical bytes when called before first
  consumption and errors when called after;
* ``FileRequestBody`` opens a fresh handle per read and holds no descriptor
  open across reads;
* ``content_length`` / ``media_type`` / ``is_replayable`` contracts stay
  consistent.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable
from io import BytesIO
from pathlib import Path

import pytest

from dexpace.sdk.core.http._once_guard import _OnceGuard
from dexpace.sdk.core.http.request import (
    AsyncRequestBody,
    FileRequestBody,
    RequestBody,
)
from dexpace.sdk.core.http.response import AsyncResponseBody, ResponseBody

_PAYLOAD = b"payload"


# ----- helpers ------------------------------------------------------------


def _drain(body: RequestBody | ResponseBody) -> bytes:
    return b"".join(body.iter_bytes())


async def _adrain(body: AsyncRequestBody | AsyncResponseBody) -> bytes:
    return b"".join([chunk async for chunk in body.aiter_bytes()])


class _AsyncBytesStream:
    """Minimal ``SupportsAsyncRead`` over an in-memory buffer."""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    async def close(self) -> object:
        self.closed = True
        return None


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ----- the guard itself ---------------------------------------------------


class TestOnceGuard:
    def test_first_claim_wins_second_raises(self) -> None:
        guard = _OnceGuard()
        guard.claim("boom")
        with pytest.raises(RuntimeError, match="boom"):
            guard.claim("boom")

    def test_race_yields_exactly_one_winner(self) -> None:
        # N threads race the very first claim. The lock — not GIL atomicity —
        # guarantees exactly one winner and N-1 RuntimeErrors.
        n = 64
        guard = _OnceGuard()
        barrier = threading.Barrier(n)
        lock = threading.Lock()
        wins = 0
        losses = 0

        def worker() -> None:
            nonlocal wins, losses
            barrier.wait(timeout=5)
            try:
                guard.claim("already claimed")
            except RuntimeError:
                with lock:
                    losses += 1
            else:
                with lock:
                    wins += 1

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert wins == 1
        assert losses == n - 1


# ----- sync request bodies ------------------------------------------------

_SyncReqFactory = Callable[[], RequestBody]

_SYNC_REQUEST_SINGLE_USE: list[tuple[str, _SyncReqFactory]] = [
    ("stream", lambda: RequestBody.from_stream(BytesIO(_PAYLOAD), content_length=7)),
    ("iter", lambda: RequestBody.from_iter([b"pay", b"load"], content_length=7)),
]


class TestSyncRequestSingleUse:
    @pytest.mark.req("BODY-08")
    @pytest.mark.parametrize(("_name", "factory"), _SYNC_REQUEST_SINGLE_USE)
    def test_double_consume_raises_documented_message(
        self, _name: str, factory: _SyncReqFactory
    ) -> None:
        body = factory()
        assert _drain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already called"):
            _drain(body)

    @pytest.mark.parametrize(("_name", "factory"), _SYNC_REQUEST_SINGLE_USE)
    def test_second_iter_bytes_raises_eagerly_without_draining(
        self, _name: str, factory: _SyncReqFactory
    ) -> None:
        body = factory()
        first = body.iter_bytes()
        with pytest.raises(RuntimeError, match="already called"):
            body.iter_bytes()
        # The single valid iterator still drains end to end.
        assert b"".join(first) == _PAYLOAD

    @pytest.mark.parametrize(("_name", "factory"), _SYNC_REQUEST_SINGLE_USE)
    def test_contracts_present_and_consistent(self, _name: str, factory: _SyncReqFactory) -> None:
        body = factory()
        assert body.is_replayable() is False
        assert body.content_length() == 7
        assert body.media_type() is None

    def test_content_length_unknown_is_minus_one(self) -> None:
        stream = RequestBody.from_stream(BytesIO(_PAYLOAD))
        iters = RequestBody.from_iter([_PAYLOAD])
        assert stream.content_length() == -1
        assert iters.content_length() == -1

    @pytest.mark.req("BODY-21")
    @pytest.mark.parametrize(("_name", "factory"), _SYNC_REQUEST_SINGLE_USE)
    def test_to_replayable_before_consumption_round_trips(
        self, _name: str, factory: _SyncReqFactory
    ) -> None:
        replayable = factory().to_replayable()
        assert replayable.is_replayable() is True
        assert _drain(replayable) == _PAYLOAD
        # Identical bytes on replay.
        assert _drain(replayable) == _PAYLOAD

    @pytest.mark.parametrize(("_name", "factory"), _SYNC_REQUEST_SINGLE_USE)
    def test_to_replayable_after_consumption_errors(
        self, _name: str, factory: _SyncReqFactory
    ) -> None:
        body = factory()
        assert _drain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already called"):
            body.to_replayable()

    def test_n_threads_racing_first_consume_one_winner(self) -> None:
        n = 32
        body = RequestBody.from_stream(BytesIO(_PAYLOAD))
        barrier = threading.Barrier(n)
        lock = threading.Lock()
        winners: list[bytes] = []
        errors: list[BaseException] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                data = b"".join(body.iter_bytes())
            except RuntimeError as exc:
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    winners.append(data)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert winners == [_PAYLOAD]
        assert len(errors) == n - 1
        assert all(isinstance(exc, RuntimeError) for exc in errors)


# ----- sync response bodies -----------------------------------------------

_SyncRespFactory = Callable[[], ResponseBody]

_SYNC_RESPONSE_SINGLE_USE: list[tuple[str, _SyncRespFactory]] = [
    ("bytes", lambda: ResponseBody.from_bytes(_PAYLOAD)),
    ("stream", lambda: ResponseBody.from_stream(BytesIO(_PAYLOAD), content_length=7)),
]


class TestSyncResponseSingleUse:
    @pytest.mark.parametrize(("_name", "factory"), _SYNC_RESPONSE_SINGLE_USE)
    def test_double_consume_raises_documented_message(
        self, _name: str, factory: _SyncRespFactory
    ) -> None:
        body = factory()
        assert _drain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already been consumed"):
            _drain(body)

    @pytest.mark.parametrize(("_name", "factory"), _SYNC_RESPONSE_SINGLE_USE)
    def test_invalid_chunk_size_raises_before_iteration(
        self, _name: str, factory: _SyncRespFactory
    ) -> None:
        # Eager validation: the ValueError must fire at call time, not on the
        # first ``next()`` of the returned iterator.
        body = factory()
        with pytest.raises(ValueError, match="chunk_size"):
            body.iter_bytes(0)

    @pytest.mark.parametrize(("_name", "factory"), _SYNC_RESPONSE_SINGLE_USE)
    def test_second_iter_bytes_raises_eagerly_without_draining(
        self, _name: str, factory: _SyncRespFactory
    ) -> None:
        body = factory()
        first = body.iter_bytes()
        with pytest.raises(RuntimeError, match="already been consumed"):
            body.iter_bytes()
        assert b"".join(first) == _PAYLOAD

    def test_n_threads_racing_first_consume_one_winner(self) -> None:
        n = 32
        body = ResponseBody.from_stream(BytesIO(_PAYLOAD))
        barrier = threading.Barrier(n)
        lock = threading.Lock()
        winners: list[bytes] = []
        errors: list[BaseException] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                data = b"".join(body.iter_bytes())
            except RuntimeError as exc:
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    winners.append(data)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert winners == [_PAYLOAD]
        assert len(errors) == n - 1


# ----- file request body ownership ----------------------------------------


class TestFileRequestBodyOwnership:
    def test_fresh_handle_per_read_reflects_mutated_file(self, tmp_path: Path) -> None:
        # A fresh handle per read means the body caches no descriptor and no
        # bytes: rewriting the file between reads is visible on the next read.
        path = tmp_path / "payload.bin"
        path.write_bytes(b"first")
        body = FileRequestBody(path)
        assert _drain(body) == b"first"
        path.write_bytes(b"second-content")
        assert _drain(body) == b"second-content"

    def test_holds_no_descriptor_open_across_reads(self, tmp_path: Path) -> None:
        # After a full read the file can be deleted and recreated — proof that
        # no caller-supplied descriptor is held open across reads.
        path = tmp_path / "payload.bin"
        path.write_bytes(b"abc")
        body = FileRequestBody(path)
        assert _drain(body) == b"abc"
        path.unlink()
        path.write_bytes(b"xyz")
        assert _drain(body) == b"xyz"

    def test_replayable_flag(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"abc")
        assert FileRequestBody(path).is_replayable() is True


# ----- async request bodies -----------------------------------------------

_AsyncReqFactory = Callable[[], AsyncRequestBody]


def _async_request_single_use() -> list[tuple[str, _AsyncReqFactory]]:
    return [
        ("stream", lambda: AsyncRequestBody.from_async_stream(_AsyncBytesStream(_PAYLOAD))),
        ("iter", lambda: AsyncRequestBody.from_async_iter(_one_chunk(_PAYLOAD))),
    ]


class TestAsyncRequestSingleUse:
    @pytest.mark.parametrize(("_name", "factory"), _async_request_single_use())
    async def test_double_consume_raises_documented_message(
        self, _name: str, factory: _AsyncReqFactory
    ) -> None:
        body = factory()
        assert await _adrain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already called"):
            await _adrain(body)

    @pytest.mark.parametrize(("_name", "factory"), _async_request_single_use())
    async def test_invalid_chunk_size_raises_before_iteration(
        self, _name: str, factory: _AsyncReqFactory
    ) -> None:
        body = factory()
        with pytest.raises(ValueError, match="chunk_size"):
            body.aiter_bytes(0)

    @pytest.mark.parametrize(("_name", "factory"), _async_request_single_use())
    async def test_second_aiter_bytes_raises_eagerly_without_draining(
        self, _name: str, factory: _AsyncReqFactory
    ) -> None:
        body = factory()
        first = body.aiter_bytes()
        with pytest.raises(RuntimeError, match="already called"):
            body.aiter_bytes()
        assert b"".join([chunk async for chunk in first]) == _PAYLOAD

    async def test_to_replayable_before_consumption_round_trips(self) -> None:
        body = AsyncRequestBody.from_async_iter(_one_chunk(_PAYLOAD))
        replayable = await body.to_replayable()
        assert replayable.is_replayable() is True
        assert await _adrain(replayable) == _PAYLOAD
        assert await _adrain(replayable) == _PAYLOAD

    async def test_to_replayable_after_consumption_errors(self) -> None:
        body = AsyncRequestBody.from_async_stream(_AsyncBytesStream(_PAYLOAD))
        assert await _adrain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already called"):
            await body.to_replayable()


# ----- async response bodies ----------------------------------------------

_AsyncRespFactory = Callable[[], AsyncResponseBody]


def _async_response_single_use() -> list[tuple[str, _AsyncRespFactory]]:
    return [
        ("bytes", lambda: AsyncResponseBody.from_bytes(_PAYLOAD)),
        ("stream", lambda: AsyncResponseBody.from_async_stream(_AsyncBytesStream(_PAYLOAD))),
    ]


class TestAsyncResponseSingleUse:
    @pytest.mark.parametrize(("_name", "factory"), _async_response_single_use())
    async def test_double_consume_raises_documented_message(
        self, _name: str, factory: _AsyncRespFactory
    ) -> None:
        body = factory()
        assert await _adrain(body) == _PAYLOAD
        with pytest.raises(RuntimeError, match="already been consumed"):
            await _adrain(body)

    @pytest.mark.parametrize(("_name", "factory"), _async_response_single_use())
    async def test_invalid_chunk_size_raises_before_iteration(
        self, _name: str, factory: _AsyncRespFactory
    ) -> None:
        body = factory()
        with pytest.raises(ValueError, match="chunk_size"):
            body.aiter_bytes(0)

    @pytest.mark.parametrize(("_name", "factory"), _async_response_single_use())
    async def test_second_aiter_bytes_raises_eagerly_without_draining(
        self, _name: str, factory: _AsyncRespFactory
    ) -> None:
        body = factory()
        first = body.aiter_bytes()
        with pytest.raises(RuntimeError, match="already been consumed"):
            body.aiter_bytes()
        assert b"".join([chunk async for chunk in first]) == _PAYLOAD
