# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Replayable ``RequestBody`` backed by a file on disk."""

from __future__ import annotations

import stat as stat_module
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from ..common.media_type import MediaType
from .request_body import RequestBody, _check_chunk_size

_DEFAULT_CHUNK: Final[int] = 64 * 1024


class FileRequestBody(RequestBody):
    """Replayable body that streams from a file on disk.

    Each ``iter_bytes`` opens the file in binary mode, seeks to ``offset``,
    and yields exactly ``content_length()`` bytes — the count declared to the
    transport as ``Content-Length``. Because the file is re-opened every call,
    the body is safely replayable under retries.

    The declared byte count is fixed at construction (from the stat-ed size),
    so if the file is truncated between construction and a read the transfer
    would deliver fewer bytes than declared. That short write is detected and
    raised naming transferred-of-total rather than completing silently with a
    truncated body.

    Transports that recognise this body type can fast-path with
    ``os.sendfile(2)`` via ``socket.sendfile`` for zero-copy delivery; the
    default ``iter_bytes`` implementation uses regular reads so the
    optimisation is transparent — transports just need to ``isinstance``-check.

    The file is stat-ed once at construction: existence, regular-file-ness, and
    the ``offset`` / ``offset + count`` window are all validated against the size
    captured then, so an invalid body fails fast rather than deferring the error
    to the first read. ``content_length`` reports from that captured size, giving
    the exact byte count the body will upload.

    Attributes:
        path: File on disk to stream.
        offset: Byte offset from the start of the file.
        count: Number of bytes to read, or ``-1`` for read-to-EOF.
    """

    __slots__ = ("_count", "_media_type", "_offset", "_path", "_size")

    def __init__(
        self,
        path: Path,
        media_type: MediaType | None = None,
        offset: int = 0,
        count: int = -1,
    ) -> None:
        """Initialise the body, validating the file and window fail-fast.

        Args:
            path: File on disk to stream.
            media_type: Optional content type.
            offset: Byte offset from the start of the file.
            count: Number of bytes to read, or ``-1`` for read-to-EOF.

        Raises:
            ValueError: If ``offset`` is negative; if ``count`` is ``0`` or less
                than ``-1``; if ``path`` does not exist or is not a regular file;
                or if ``offset`` (or ``offset + count``) exceeds the size the
                file has at construction.
        """
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if count < -1 or count == 0:
            raise ValueError(f"count must be -1 (read to EOF) or positive, got {count}")
        size = _validate_file(path)
        if offset > size:
            raise ValueError(f"offset {offset} is past the file size {size}: {path}")
        if count != -1 and offset + count > size:
            raise ValueError(f"offset+count {offset + count} exceeds the file size {size}: {path}")
        self._path = path
        self._media_type = media_type
        self._offset = offset
        self._count = count
        self._size = size

    @property
    def path(self) -> Path:
        return self._path

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def count(self) -> int:
        return self._count

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        # Report from the size captured at construction — validation already
        # guaranteed the whole window fits, so this is the exact upload size.
        if self._count == -1:
            return self._size - self._offset
        return self._count

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self) -> RequestBody:
        return self

    def iter_bytes(self, chunk_size: int = _DEFAULT_CHUNK) -> Iterator[bytes]:
        _check_chunk_size(chunk_size)
        return self._iter(chunk_size)

    def _iter(self, chunk_size: int) -> Iterator[bytes]:
        # Bound the transfer to the declared content length (fixed at
        # construction). Hitting EOF before delivering it means the file was
        # truncated under us: raise naming transferred-of-total rather than
        # emitting a truncated body.
        total = self.content_length()
        remaining = total
        with self._path.open("rb") as stream:
            if self._offset:
                stream.seek(self._offset)
            while remaining > 0:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise OSError(
                        f"short read from {self._path}: transferred "
                        f"{total - remaining} of {total} bytes before end of "
                        f"file (was the file truncated after construction?)"
                    )
                remaining -= len(chunk)
                yield chunk


def _validate_file(path: Path) -> int:
    """Stat ``path`` fail-fast and return its size in bytes.

    Args:
        path: File the body will stream from.

    Returns:
        The regular file's size, in bytes, as observed at this call.

    Raises:
        ValueError: If ``path`` does not exist / cannot be stat-ed, or names a
            directory or other non-regular file.
    """
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"file does not exist or is not accessible: {path}") from exc
    if not stat_module.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path}")
    return info.st_size


__all__ = ["FileRequestBody"]
