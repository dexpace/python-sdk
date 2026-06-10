# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``RequestBody`` factories and the ``LoggableRequestBody`` / ``FileRequestBody`` wrappers."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.http.request import (
    FileRequestBody,
    LoggableRequestBody,
    RequestBody,
)


def _drain(body: RequestBody) -> bytes:
    return b"".join(body.iter_bytes())


class TestFactories:
    def test_from_bytes(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        assert body.is_replayable()
        assert body.content_length() == 7
        assert _drain(body) == b"payload"
        # Replayable — can be drained twice.
        assert _drain(body) == b"payload"

    def test_from_string(self) -> None:
        body = RequestBody.from_string("hello")
        assert _drain(body) == b"hello"

    def test_from_form(self) -> None:
        body = RequestBody.from_form({"a": "1", "b": "two words"})
        text = _drain(body).decode()
        assert "a=1" in text and "b=two%20words" in text
        assert body.media_type() == common_media_types.APPLICATION_FORM_URLENCODED

    def test_from_form_encoding_changes_percent_encoding(self) -> None:
        # A non-ASCII field must percent-encode through the requested charset,
        # so latin-1 and utf-8 produce different bytes (one byte vs two for é).
        fields = {"name": "é"}
        latin1 = _drain(RequestBody.from_form(fields, encoding="latin-1"))
        utf8 = _drain(RequestBody.from_form(fields, encoding="utf-8"))
        assert latin1 == b"name=%E9"
        assert utf8 == b"name=%C3%A9"
        assert latin1 != utf8

    def test_from_stream_single_use(self) -> None:
        body = RequestBody.from_stream(BytesIO(b"once"), content_length=4)
        assert not body.is_replayable()
        assert _drain(body) == b"once"
        with pytest.raises(RuntimeError):
            _drain(body)

    def test_from_iter_single_use(self) -> None:
        body = RequestBody.from_iter([b"a", b"bc"], content_length=3)
        assert _drain(body) == b"abc"
        with pytest.raises(RuntimeError):
            _drain(body)

    def test_to_replayable_buffers_stream(self) -> None:
        body = RequestBody.from_stream(BytesIO(b"data")).to_replayable()
        assert body.is_replayable()
        assert _drain(body) == b"data"
        assert _drain(body) == b"data"


class TestWriteTo:
    def test_write_to_returns_total(self) -> None:
        body = RequestBody.from_bytes(b"abcdefg")
        sink = BytesIO()
        total = body.write_to(sink)
        assert total == 7
        assert sink.getvalue() == b"abcdefg"


class TestLoggableRequestBody:
    def test_passes_through_to_primary(self) -> None:
        logged = LoggableRequestBody(RequestBody.from_bytes(b"hello world"))
        sink = BytesIO()
        logged.write_to(sink)
        assert sink.getvalue() == b"hello world"

    def test_captures_to_snapshot(self) -> None:
        logged = LoggableRequestBody(RequestBody.from_bytes(b"capture me"))
        list(logged.iter_bytes())
        assert logged.snapshot() == b"capture me"

    def test_invalid_cap_raises(self) -> None:
        with pytest.raises(ValueError):
            LoggableRequestBody(RequestBody.from_bytes(b""), max_capture_bytes=0)

    def test_cap_truncates_capture_not_primary(self) -> None:
        logged = LoggableRequestBody(
            RequestBody.from_bytes(b"abcdefghij"),
            max_capture_bytes=4,
        )
        sink = BytesIO()
        logged.write_to(sink)
        # Primary still receives the full payload.
        assert sink.getvalue() == b"abcdefghij"
        # Capture is capped.
        assert len(logged.snapshot()) <= 4


class TestFileRequestBody:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"file contents")
        body = FileRequestBody(path)
        assert body.is_replayable()
        assert body.content_length() == 13
        assert _drain(body) == b"file contents"
        # Replayable — re-opens the file.
        assert _drain(body) == b"file contents"

    def test_offset_and_count(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"0123456789")
        body = FileRequestBody(path, offset=2, count=5)
        assert body.content_length() == 5
        assert _drain(body) == b"23456"

    def test_content_length_clamps_count_past_eof(self, tmp_path: Path) -> None:
        # Requesting more bytes than the file holds must not over-report:
        # iter_bytes stops at EOF, so content_length must match the drained size.
        path = tmp_path / "short.bin"
        path.write_bytes(b"0123456789")  # 10 bytes
        body = FileRequestBody(path, offset=4, count=1000)
        drained = _drain(body)
        assert drained == b"456789"  # only 6 bytes available past the offset
        assert body.content_length() == len(drained) == 6

    def test_content_length_falls_back_to_count_when_stat_fails(self, tmp_path: Path) -> None:
        # When stat raises (e.g. the file does not exist yet), fall back to the
        # requested count rather than guessing zero.
        body = FileRequestBody(tmp_path / "missing.bin", count=7)
        assert body.content_length() == 7

    def test_from_file_factory(self, tmp_path: Path) -> None:
        path = tmp_path / "x.bin"
        path.write_bytes(b"abc")
        body = RequestBody.from_file(path)
        assert isinstance(body, FileRequestBody)
        assert _drain(body) == b"abc"

    def test_negative_offset_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            FileRequestBody(tmp_path / "x", offset=-1)

    def test_zero_count_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            FileRequestBody(tmp_path / "x", count=0)


def _make_file_body(tmp_path: Path) -> RequestBody:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"hi")
    return FileRequestBody(path)


@pytest.mark.parametrize(
    "factory",
    [
        lambda _tmp: RequestBody.from_bytes(b"hi"),
        lambda _tmp: RequestBody.from_string("hi"),
        lambda _tmp: RequestBody.from_form({"a": "1"}),
        lambda _tmp: RequestBody.from_iter([b"hi"]),
        lambda _tmp: RequestBody.from_stream(BytesIO(b"hi")),
        _make_file_body,
    ],
)
@pytest.mark.parametrize("size", [0, -1])
def test_iter_bytes_rejects_invalid_chunk_size(
    factory: Callable[[Path], RequestBody],
    size: int,
    tmp_path: Path,
) -> None:
    body = factory(tmp_path)
    with pytest.raises(ValueError, match="chunk_size"):
        list(body.iter_bytes(size))


class TestEagerIterBytesValidation:
    """``iter_bytes`` must validate at call time, not on first ``next()``.

    A generator-function ``iter_bytes`` defers its argument checks and the
    consumed-flag flip to the first iteration step, so the documented
    ``ValueError`` / ``RuntimeError`` only surfaces once the caller starts
    pulling chunks. The fix wraps each body's generator behind a thin
    validating function so the errors fire as soon as ``iter_bytes`` is
    called.
    """

    @pytest.mark.parametrize(
        "factory",
        [
            lambda _tmp: RequestBody.from_bytes(b"hi"),
            lambda _tmp: RequestBody.from_string("hi"),
            lambda _tmp: RequestBody.from_form({"a": "1"}),
            lambda _tmp: RequestBody.from_iter([b"hi"]),
            lambda _tmp: RequestBody.from_stream(BytesIO(b"hi")),
            _make_file_body,
        ],
    )
    @pytest.mark.parametrize("size", [0, -1])
    def test_invalid_chunk_size_raises_before_iteration(
        self,
        factory: Callable[[Path], RequestBody],
        size: int,
        tmp_path: Path,
    ) -> None:
        # Calling iter_bytes() must raise immediately — without ever calling
        # next() on the returned iterator.
        body = factory(tmp_path)
        with pytest.raises(ValueError, match="chunk_size"):
            body.iter_bytes(size)

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: RequestBody.from_iter([b"hi"]),
            lambda: RequestBody.from_stream(BytesIO(b"hi")),
        ],
    )
    def test_consumed_flag_flips_eagerly_on_call(
        self,
        factory: Callable[[], RequestBody],
    ) -> None:
        # Two un-iterated generators must not both believe the body is fresh:
        # the second iter_bytes() call must raise RuntimeError at call time,
        # even though neither iterator has been advanced. This closes the race
        # where two undrained generators share the consumed flag.
        body = factory()
        first = body.iter_bytes()
        with pytest.raises(RuntimeError, match="already called"):
            body.iter_bytes()
        # The first (valid) iterator still works end to end.
        assert b"".join(first) == b"hi"

    def test_stream_not_consumed_when_chunk_size_invalid(self) -> None:
        # An eager ValueError must not flip the single-use consumed flag, so a
        # follow-up valid call still works.
        body = RequestBody.from_stream(BytesIO(b"payload"))
        with pytest.raises(ValueError, match="chunk_size"):
            body.iter_bytes(0)
        assert b"".join(body.iter_bytes()) == b"payload"
