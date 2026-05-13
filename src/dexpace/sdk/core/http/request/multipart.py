"""Multipart/form-data ``RequestBody`` builder.

Generates a deterministic-boundary ``multipart/form-data`` payload from a
list of fields. Each field has a name, value (bytes or string), optional
filename, optional media type, and optional extra headers.

The resulting body is replayable (the boundary and field bytes are
captured once at construction), so retries are safe.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import quote

from ..common.media_type import MediaType
from .request_body import RequestBody, _check_chunk_size


def _is_ascii(value: str) -> bool:
    """Return ``True`` if ``value`` is pure ASCII (the safe HTTP header subset).

    Although Latin-1 is the formal HTTP/1.1 header charset, many servers
    and proxies still choke on bytes ``>= 0x80``. RFC 7578 §5.1 recommends
    restricting multipart field names and filenames to US-ASCII and
    escaping anything else via RFC 5987 (``filename*=UTF-8''…``).
    """
    return value.isascii()


def _has_filename_star_header(headers: Sequence[tuple[str, str]]) -> bool:
    """Return ``True`` if any header value mentions ``filename*=``."""
    return any("filename*=" in v for _, v in headers)


@dataclass(frozen=True, slots=True)
class MultipartField:
    """One part of a ``multipart/form-data`` body.

    Attributes:
        name: Form field name (mandatory). Must be pure ASCII so the
            generated ``Content-Disposition`` header is safe across the
            full range of HTTP/1.1 parsers.
        value: Field content as bytes or string. Strings are UTF-8 encoded.
        filename: Optional filename for file parts; included in
            ``Content-Disposition``. Must be pure ASCII unless the caller
            has supplied a matching ``filename*`` parameter (e.g. via
            ``with_utf8_filename`` or a custom header).
        media_type: Optional content type for the part.
        headers: Optional extra headers as ``(name, value)`` pairs.

    Raises:
        ValueError: If ``name`` is not ASCII, or if ``filename`` is not
            ASCII and no ``filename*=`` parameter was provided through
            ``headers``.
    """

    name: str
    value: bytes | str
    filename: str | None = None
    media_type: MediaType | None = None
    headers: Sequence[tuple[str, str]] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _is_ascii(self.name):
            raise ValueError(f"multipart field name must be pure ASCII: {self.name!r}")
        if (
            self.filename is not None
            and not _is_ascii(self.filename)
            and not _has_filename_star_header(self.headers)
        ):
            raise ValueError(
                "multipart filename is not ASCII; use "
                "MultipartField.with_utf8_filename(...) or supply a "
                f"filename*=UTF-8''… header: {self.filename!r}"
            )

    @classmethod
    def with_utf8_filename(
        cls,
        *,
        name: str,
        value: bytes | str,
        filename: str,
        media_type: MediaType | None = None,
        headers: Sequence[tuple[str, str]] = (),
        ascii_fallback: str = "file",
    ) -> Self:
        """Construct a field whose filename is rendered as RFC 5987.

        The emitted ``Content-Disposition`` will carry both a legacy
        ``filename="…"`` parameter (using ``ascii_fallback`` so older
        parsers see something stable) and a ``filename*=UTF-8''…``
        parameter holding the percent-encoded UTF-8 form of ``filename``.

        Args:
            name: Form field name (must be ASCII).
            value: Field content as bytes or string.
            filename: The intended filename, possibly containing
                non-ASCII characters.
            media_type: Optional content type for the part.
            headers: Optional extra headers as ``(name, value)`` pairs.
            ascii_fallback: ASCII filename used in the legacy
                ``filename="…"`` parameter when ``filename`` contains
                non-ASCII characters. Defaults to ``"file"``.

        Returns:
            A ``MultipartField`` with the synthesised disposition stored
            as an extra header. The dataclass ``filename`` attribute is
            set to ``None`` so ``_build_part`` does not emit a second
            disposition line.

        Raises:
            ValueError: If ``name`` is not ASCII.
        """
        legacy = filename if _is_ascii(filename) else ascii_fallback
        encoded = quote(filename, safe="", encoding="utf-8")
        disposition = (
            f'form-data; name="{_escape_quoted(name)}"; '
            f'filename="{_escape_quoted(legacy)}"; '
            f"filename*=UTF-8''{encoded}"
        )
        new_headers: tuple[tuple[str, str], ...] = (
            ("Content-Disposition", disposition),
            *tuple(headers),
        )
        return cls(
            name=name,
            value=value,
            filename=None,
            media_type=media_type,
            headers=new_headers,
        )


def _generate_boundary() -> str:
    """Return a random RFC 2046 multipart boundary."""
    return "----dexpace-" + secrets.token_hex(16)


def _build_part(part: MultipartField, boundary: str) -> bytes:
    """Render one part as bytes (terminating CRLF included).

    Header lines are encoded as Latin-1 (the HTTP/1.1 wire-form charset).
    ``MultipartField.__post_init__`` rejects names/filenames that are not
    pure ASCII (unless the caller supplied a matching ``filename*=UTF-8''…``
    parameter via ``headers`` or built the field through
    ``with_utf8_filename``).

    If any caller-supplied header already begins with ``Content-Disposition``
    (case-insensitive), the auto-generated disposition is suppressed so the
    custom one (typically carrying ``filename*=UTF-8''…``) is the only one
    emitted.
    """
    custom_disposition = any(name.lower() == "content-disposition" for name, _ in part.headers)
    lines: list[bytes] = [f"--{boundary}".encode("latin-1")]
    if not custom_disposition:
        disposition = f'form-data; name="{_escape_quoted(part.name)}"'
        if part.filename is not None:
            disposition += f'; filename="{_escape_quoted(part.filename)}"'
        lines.append(f"Content-Disposition: {disposition}".encode("latin-1"))
    if part.media_type is not None:
        lines.append(f"Content-Type: {part.media_type}".encode("latin-1"))
    for header_name, header_value in part.headers:
        lines.append(f"{header_name}: {header_value}".encode("latin-1"))
    lines.append(b"")
    if isinstance(part.value, str):
        lines.append(part.value.encode("utf-8"))
    else:
        lines.append(part.value)
    return b"\r\n".join(lines) + b"\r\n"


def _escape_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MultipartRequestBody(RequestBody):
    """Replayable ``multipart/form-data`` body.

    Build via ``RequestBody.from_multipart(fields)`` or instantiate directly.
    The boundary is generated once at construction so retries see identical
    bytes (and so loggable wrappers can capture the payload deterministically).
    """

    __slots__ = ("_boundary", "_payload")

    def __init__(
        self,
        fields: Sequence[MultipartField],
        *,
        boundary: str | None = None,
    ) -> None:
        if not fields:
            raise ValueError("at least one field is required")
        self._boundary = boundary or _generate_boundary()
        parts: list[bytes] = [_build_part(f, self._boundary) for f in fields]
        parts.append(f"--{self._boundary}--\r\n".encode("ascii"))
        self._payload = b"".join(parts)

    @property
    def boundary(self) -> str:
        return self._boundary

    def media_type(self) -> MediaType | None:
        return MediaType.of("multipart", "form-data", {"boundary": self._boundary})

    def content_length(self) -> int:
        return len(self._payload)

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self) -> RequestBody:
        return self

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        _check_chunk_size(chunk_size)
        view = memoryview(self._payload)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])


def _from_multipart(
    cls: type[RequestBody],
    fields: Sequence[MultipartField],
    *,
    boundary: str | None = None,
) -> RequestBody:
    del cls
    return MultipartRequestBody(fields, boundary=boundary)


RequestBody.from_multipart = classmethod(_from_multipart)  # type: ignore[attr-defined]


__all__ = ["MultipartField", "MultipartRequestBody"]
