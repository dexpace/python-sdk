"""Tests for ``RequestBody`` factories and the ``LoggableRequestBody`` / ``FileRequestBody`` wrappers."""
from __future__ import annotations

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

    def test_from_file_factory(self, tmp_path: Path) -> None:
        path = tmp_path / "x.bin"
        path.write_bytes(b"abc")
        body = RequestBody.from_file(path)  # type: ignore[attr-defined]  # added dynamically in file_request_body
        assert isinstance(body, FileRequestBody)
        assert _drain(body) == b"abc"

    def test_negative_offset_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            FileRequestBody(tmp_path / "x", offset=-1)

    def test_zero_count_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            FileRequestBody(tmp_path / "x", count=0)
