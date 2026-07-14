# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Shared-framing, preview, and replayability-propagation tests for multipart.

These cover PY-M2-06's acceptance criteria: wire bytes equal the shared-framing
preview bytes (and the logging tap that consumes them), replayability
propagates from the fields, and ``content_length`` matches the drained bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from dexpace.sdk.core.http.request import (
    LoggableRequestBody,
    MultipartField,
    MultipartRequestBody,
    RequestBody,
)
from dexpace.sdk.core.http.request import _multipart_framing as framing


def _drain(body: RequestBody) -> bytes:
    return b"".join(body.iter_bytes())


# --- Shared framing: wire bytes == preview bytes ---------------------------


def test_wire_bytes_equal_shared_framing_preview() -> None:
    # The wire path and the preview both consume ``iter_frames`` via the same
    # module, so a full render must equal a full drain byte for byte.
    fields = [
        MultipartField(name="text", value="hello world"),
        MultipartField(
            name="file",
            value=b"<<binary>>",
            filename="upload.bin",
        ),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    wire = _drain(body)
    preview = framing.render_frames(body.fields, body.boundary)
    assert wire == preview


def test_tap_snapshot_equals_wire_via_shared_framing() -> None:
    # The logging tap captures exactly what goes on the wire; asserting the tap
    # snapshot against the shared-framing render ties the two together.
    fields = [MultipartField(name="a", value="b"), MultipartField(name="c", value=b"d")]
    body = MultipartRequestBody(fields, boundary="BND")
    logged = LoggableRequestBody(body)
    wire = _drain(logged)
    assert logged.snapshot() == wire
    assert wire == framing.render_frames(body.fields, body.boundary)


def test_preview_is_truncatable() -> None:
    body = MultipartRequestBody([MultipartField(name="a", value="b")], boundary="BND")
    full = framing.render_frames(body.fields, body.boundary)
    capped = framing.render_frames(body.fields, body.boundary, max_bytes=8)
    assert capped == full[:8]


def test_preview_negative_cap_rejected() -> None:
    body = MultipartRequestBody([MultipartField(name="a", value="b")], boundary="BND")
    with pytest.raises(ValueError, match="max_bytes"):
        framing.render_frames(body.fields, body.boundary, max_bytes=-1)


def test_chunk_size_does_not_change_bytes() -> None:
    body = MultipartRequestBody(
        [MultipartField(name="big", value=b"0123456789" * 20)],
        boundary="BND",
    )
    small = b"".join(body.iter_bytes(4))
    large = b"".join(body.iter_bytes(4096))
    assert small == large == framing.render_frames(body.fields, body.boundary)


# --- Replayability-propagation matrix --------------------------------------


def _replayable_request_body(tmp_path: Path) -> RequestBody:
    path = tmp_path / "part.bin"
    path.write_bytes(b"file-part")
    return RequestBody.from_file(path)


def test_all_replayable_fields_make_body_replayable(tmp_path: Path) -> None:
    fields = [
        MultipartField(name="s", value="str"),
        MultipartField(name="b", value=b"bytes"),
        MultipartField(name="rb", value=RequestBody.from_bytes(b"nested")),
        MultipartField(name="file", value=_replayable_request_body(tmp_path)),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    assert body.is_replayable()
    # Replayable bodies survive being drained twice with identical bytes.
    assert _drain(body) == _drain(body)
    assert body.to_replayable() is body


@pytest.mark.parametrize(
    "single_use_factory",
    [
        lambda: RequestBody.from_stream(BytesIO(b"streamed")),
        lambda: RequestBody.from_iter([b"chunk-a", b"chunk-b"]),
    ],
)
def test_any_non_replayable_field_makes_body_non_replayable(
    single_use_factory: Callable[[], RequestBody],
) -> None:
    fields = [
        MultipartField(name="ok", value="replayable"),
        MultipartField(name="stream", value=single_use_factory()),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    assert not body.is_replayable()


def test_non_replayable_body_second_iter_raises() -> None:
    fields = [
        MultipartField(name="ok", value="replayable"),
        MultipartField(name="stream", value=RequestBody.from_stream(BytesIO(b"once"))),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    first = _drain(body)
    assert b"once" in first
    with pytest.raises(RuntimeError, match="already called"):
        body.iter_bytes()


def test_non_replayable_second_iter_raises_eagerly() -> None:
    # The RuntimeError must fire at call time, before the returned iterator is
    # advanced, so a retry loop learns the body is spent immediately.
    body = MultipartRequestBody(
        [MultipartField(name="stream", value=RequestBody.from_iter([b"x"]))],
        boundary="BND",
    )
    body.iter_bytes()
    with pytest.raises(RuntimeError, match="already called"):
        body.iter_bytes()


def test_invalid_chunk_size_does_not_consume_single_use_body() -> None:
    body = MultipartRequestBody(
        [MultipartField(name="stream", value=RequestBody.from_stream(BytesIO(b"payload")))],
        boundary="BND",
    )
    with pytest.raises(ValueError, match="chunk_size"):
        body.iter_bytes(0)
    # The eager ValueError must not flip the consumed flag.
    assert b"payload" in _drain(body)


def test_to_replayable_buffers_single_use_multipart() -> None:
    fields = [
        MultipartField(name="ok", value="replayable"),
        MultipartField(name="stream", value=RequestBody.from_stream(BytesIO(b"streamed"))),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    buffered = body.to_replayable()
    assert buffered.is_replayable()
    # The buffered copy keeps the multipart media type and replays identically.
    media = buffered.media_type()
    assert media is not None and media.full_type == "multipart/form-data"
    assert b"".join(buffered.iter_bytes()) == b"".join(buffered.iter_bytes())
    assert b"streamed" in b"".join(buffered.iter_bytes())


def test_nested_request_body_streams_into_part() -> None:
    body = MultipartRequestBody(
        [
            MultipartField(
                name="file",
                value=RequestBody.from_bytes(b"nested-payload"),
                filename="a.bin",
            )
        ],
        boundary="BND",
    )
    drained = _drain(body)
    assert b'filename="a.bin"' in drained
    assert b"nested-payload" in drained


# --- content_length --------------------------------------------------------


def test_content_length_matches_drain_for_known_fields(tmp_path: Path) -> None:
    fields = [
        MultipartField(name="s", value="hello"),
        MultipartField(name="b", value=b"world!!"),
        MultipartField(name="file", value=_replayable_request_body(tmp_path)),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    assert body.content_length() == len(_drain(body))


def test_content_length_unknown_when_stream_field_present() -> None:
    fields = [
        MultipartField(name="ok", value="known"),
        MultipartField(name="stream", value=RequestBody.from_stream(BytesIO(b"x"))),
    ]
    body = MultipartRequestBody(fields, boundary="BND")
    assert body.content_length() == -1


def test_empty_value_produces_zero_length_part() -> None:
    body = MultipartRequestBody([MultipartField(name="empty", value=b"")], boundary="BND")
    drained = _drain(body)
    assert b'name="empty"' in drained
    assert body.content_length() == len(drained)
