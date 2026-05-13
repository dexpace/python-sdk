"""Tests for ``MultipartRequestBody``."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import MediaType
from dexpace.sdk.core.http.request import (
    MultipartField,
    MultipartRequestBody,
    RequestBody,
)


def _drain(body: RequestBody) -> bytes:
    return b"".join(body.iter_bytes())


def test_simple_field() -> None:
    body = MultipartRequestBody([MultipartField(name="key", value="value")])
    drained = _drain(body)
    assert b'name="key"' in drained
    assert b"value" in drained


def test_filename_in_disposition() -> None:
    body = MultipartRequestBody(
        [
            MultipartField(
                name="file",
                value=b"<<bytes>>",
                filename="upload.bin",
                media_type=MediaType.of("application", "octet-stream"),
            )
        ]
    )
    drained = _drain(body)
    assert b'filename="upload.bin"' in drained
    assert b"Content-Type: application/octet-stream" in drained


def test_media_type_includes_boundary() -> None:
    body = MultipartRequestBody([MultipartField(name="a", value="b")])
    media = body.media_type()
    assert media is not None and media.full_type == "multipart/form-data"
    assert dict(media.parameters)["boundary"] == body.boundary


def test_replayable() -> None:
    body = MultipartRequestBody([MultipartField(name="a", value="b")])
    assert body.is_replayable()
    assert _drain(body) == _drain(body)


def test_factory() -> None:
    body = RequestBody.from_multipart(  # type: ignore[attr-defined]
        [MultipartField(name="a", value="b")]
    )
    assert isinstance(body, MultipartRequestBody)


def test_empty_fields_raises() -> None:
    with pytest.raises(ValueError):
        MultipartRequestBody([])


def test_explicit_boundary() -> None:
    body = MultipartRequestBody(
        [MultipartField(name="a", value="b")],
        boundary="my-boundary",
    )
    drained = _drain(body)
    assert b"--my-boundary" in drained
    assert b"--my-boundary--" in drained


def test_quotes_in_name_escaped() -> None:
    body = MultipartRequestBody(
        [MultipartField(name='odd"name', value="x")],
    )
    drained = _drain(body)
    assert b'\\"name' in drained


@pytest.mark.parametrize("size", [0, -1])
def test_iter_bytes_rejects_invalid_chunk_size(size: int) -> None:
    body = MultipartRequestBody([MultipartField(name="a", value="b")])
    with pytest.raises(ValueError, match="chunk_size"):
        list(body.iter_bytes(size))


def test_non_latin1_name_rejected() -> None:
    with pytest.raises(ValueError, match="name"):
        MultipartField(name="naïve", value=b"v")


def test_non_latin1_filename_rejected_without_filename_star() -> None:
    with pytest.raises(ValueError, match="filename"):
        MultipartField(name="file", value=b"v", filename="résumé.pdf")


def test_non_latin1_filename_accepted_when_filename_star_provided() -> None:
    # When caller explicitly provides a filename* header, the validator
    # trusts them and accepts the non-Latin-1 filename.
    field = MultipartField(
        name="file",
        value=b"v",
        filename="résumé.pdf",
        headers=(("Content-Disposition-Extra", "filename*=UTF-8''resume.pdf"),),
    )
    assert field.filename == "résumé.pdf"


def test_with_utf8_filename_succeeds() -> None:
    field = MultipartField.with_utf8_filename(
        name="file", value=b"<<bytes>>", filename="résumé.pdf"
    )
    body = MultipartRequestBody([field])
    drained = _drain(body)
    # RFC 5987 percent-encoded form must appear (é = %C3%A9).
    assert b"filename*=UTF-8''r%C3%A9sum%C3%A9.pdf" in drained
    # An ASCII fallback filename= parameter must also be present so legacy
    # parsers still see something.
    assert b'filename="' in drained


def test_ascii_filename_still_works() -> None:
    body = MultipartRequestBody([MultipartField(name="file", value=b"x", filename="upload.bin")])
    drained = _drain(body)
    assert b'filename="upload.bin"' in drained
