# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure framing routine for ``multipart/form-data`` payloads.

This module holds the single source of truth for how a multipart body is laid
out on the wire: the ``--boundary`` delimiters, the per-part header block, the
UTF-8/Latin-1 encoding rules, and the closing delimiter. Both the wire-write
path (``MultipartRequestBody.iter_bytes``) and any logging-tap preview consume
these functions, so the bytes a caller logs and the bytes put on the wire can
never diverge.

The functions are pure and stateless: they read a field's data attributes and
emit bytes, holding no mutable state of their own. Single-use enforcement for
non-replayable field payloads lives on the owning body, not here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from .request_body import RequestBody

if TYPE_CHECKING:
    from .multipart import MultipartField

_CRLF = b"\r\n"


def escape_quoted(value: str) -> str:
    """Backslash-escape ``\\`` and ``"`` for an RFC 2045 quoted-string.

    Args:
        value: Raw parameter value to place inside double quotes.

    Returns:
        The value with backslashes and double quotes escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def part_header_block(part: MultipartField, boundary: str) -> bytes:
    """Render the delimiter and header lines that precede a part's payload.

    Emits everything up to (and including) the blank line that separates the
    part headers from the part body: the ``--boundary`` delimiter, the header
    lines, then a terminating blank line. Header lines are Latin-1 encoded (the
    HTTP/1.1 wire charset); ``MultipartField.__post_init__`` has already
    rejected non-ASCII names/filenames and any CR/LF/NUL.

    Args:
        part: The field whose header block is being rendered.
        boundary: The multipart boundary token.

    Returns:
        The part's header block as bytes, ending in a blank ``CRLF`` line.
    """
    custom_disposition = any(name.lower() == "content-disposition" for name, _ in part.headers)
    lines: list[bytes] = [f"--{boundary}".encode("latin-1")]
    if not custom_disposition:
        disposition = f'form-data; name="{escape_quoted(part.name)}"'
        if part.filename is not None:
            disposition += f'; filename="{escape_quoted(part.filename)}"'
        lines.append(f"Content-Disposition: {disposition}".encode("latin-1"))
    if part.media_type is not None:
        lines.append(f"Content-Type: {part.media_type}".encode("latin-1"))
    for header_name, header_value in part.headers:
        lines.append(f"{header_name}: {header_value}".encode("latin-1"))
    return _CRLF.join(lines) + _CRLF + _CRLF


def closing_delimiter(boundary: str) -> bytes:
    """Return the terminating ``--boundary--`` delimiter line (with ``CRLF``)."""
    return f"--{boundary}--".encode("latin-1") + _CRLF


def iter_value_bytes(value: bytes | str | RequestBody, chunk_size: int) -> Iterator[bytes]:
    """Yield a field payload's bytes, chunked for the wire.

    ``str`` values are UTF-8 encoded; ``bytes`` are chunked in place; a
    ``RequestBody`` value is streamed through its own ``iter_bytes`` (which
    enforces single-use semantics for non-replayable bodies). An empty value
    yields nothing, producing a correct zero-length part body.

    Args:
        value: The field payload.
        chunk_size: Suggested chunk size for bytes/str payloads and the hint
            forwarded to a ``RequestBody`` payload.

    Yields:
        Successive ``bytes`` chunks of the payload.
    """
    if isinstance(value, RequestBody):
        yield from value.iter_bytes(chunk_size)
        return
    raw = value.encode("utf-8") if isinstance(value, str) else value
    view = memoryview(raw)
    for start in range(0, len(view), chunk_size):
        yield bytes(view[start : start + chunk_size])


def iter_frames(
    fields: Sequence[MultipartField],
    boundary: str,
    chunk_size: int,
) -> Iterator[bytes]:
    """Yield the full ``multipart/form-data`` wire bytes for ``fields``.

    This is *the* framing routine. The wire-write path and the logging-tap
    preview both drive it, so the two can never disagree on a single byte.

    Args:
        fields: Ordered multipart fields.
        boundary: The multipart boundary token.
        chunk_size: Suggested chunk size forwarded to each field payload.

    Yields:
        Successive ``bytes`` chunks of the complete framed payload.
    """
    for part in fields:
        yield part_header_block(part, boundary)
        yield from iter_value_bytes(part.value, chunk_size)
        yield _CRLF
    yield closing_delimiter(boundary)


def value_length(value: bytes | str | RequestBody) -> int:
    """Return the byte length of a field payload, or ``-1`` if unknown.

    Args:
        value: The field payload.

    Returns:
        The payload's byte count, or ``-1`` when a ``RequestBody`` payload
        reports an unknown ``content_length``.
    """
    if isinstance(value, RequestBody):
        return value.content_length()
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(value)


def content_length(fields: Sequence[MultipartField], boundary: str) -> int:
    """Return the total framed byte length, or ``-1`` if any part is unknown.

    Computed from the same header-block and delimiter rendering that
    ``iter_frames`` uses, so the advertised length matches the bytes produced
    exactly whenever every field length is known.

    Args:
        fields: Ordered multipart fields.
        boundary: The multipart boundary token.

    Returns:
        The total byte count, or ``-1`` when any field payload has an unknown
        length.
    """
    total = len(closing_delimiter(boundary))
    for part in fields:
        total += len(part_header_block(part, boundary))
        vlen = value_length(part.value)
        if vlen < 0:
            return -1
        total += vlen + len(_CRLF)
    return total


def render_frames(
    fields: Sequence[MultipartField],
    boundary: str,
    *,
    max_bytes: int | None = None,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Materialise the framed payload, optionally truncated to ``max_bytes``.

    This is the logging-tap-consumable preview: it produces exactly the bytes
    ``iter_frames`` puts on the wire (they share the routine), capped at
    ``max_bytes`` when given. Consuming this drains any non-replayable field
    payloads, so callers should preview replayable bodies only.

    Args:
        fields: Ordered multipart fields.
        boundary: The multipart boundary token.
        max_bytes: Optional cap; at most this many leading bytes are returned.
        chunk_size: Suggested chunk size forwarded to each field payload.

    Returns:
        The framed payload bytes, truncated to ``max_bytes`` when supplied.

    Raises:
        ValueError: If ``max_bytes`` is negative.
    """
    if max_bytes is not None and max_bytes < 0:
        raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
    out = bytearray()
    for chunk in iter_frames(fields, boundary, chunk_size):
        if max_bytes is None:
            out += chunk
            continue
        remaining = max_bytes - len(out)
        if remaining <= 0:
            break
        out += chunk[:remaining]
    return bytes(out)


__all__ = [
    "closing_delimiter",
    "content_length",
    "escape_quoted",
    "iter_frames",
    "iter_value_bytes",
    "part_header_block",
    "render_frames",
    "value_length",
]
