"""Replayable ``RequestBody`` backed by a file on disk."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

from ..common.media_type import MediaType
from .request_body import RequestBody

_DEFAULT_CHUNK: Final[int] = 64 * 1024


class FileRequestBody(RequestBody):
    """Replayable body that streams from a file on disk.

    Each ``iter_bytes`` opens the file in binary mode, seeks to ``offset``,
    and yields up to ``count`` bytes (or to EOF when ``count == -1``).
    Because the file is re-opened every call, the body is safely replayable
    under retries.

    Transports that recognise this body type can fast-path with
    ``os.sendfile(2)`` via ``socket.sendfile`` for zero-copy delivery; the
    default ``iter_bytes`` implementation uses regular reads so the
    optimisation is transparent — transports just need to ``isinstance``-check.

    Attributes:
        path: File on disk to stream.
        offset: Byte offset from the start of the file.
        count: Number of bytes to read, or ``-1`` for read-to-EOF.
    """

    __slots__ = ("_count", "_media_type", "_offset", "_path")

    def __init__(
        self,
        path: Path,
        media_type: MediaType | None = None,
        offset: int = 0,
        count: int = -1,
    ) -> None:
        """Initialise the body.

        Args:
            path: File on disk to stream.
            media_type: Optional content type.
            offset: Byte offset from the start of the file.
            count: Number of bytes to read, or ``-1`` for read-to-EOF.

        Raises:
            ValueError: If ``offset`` is negative or ``count`` is ``0``
                or less than ``-1``.
        """
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if count < -1 or count == 0:
            raise ValueError(
                f"count must be -1 (read to EOF) or positive, got {count}"
            )
        self._path = path
        self._media_type = media_type
        self._offset = offset
        self._count = count

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
        if self._count != -1:
            return self._count
        # Stat lazily — the file may grow between body construction and send;
        # whatever stat returns at call time is the best estimate we have.
        try:
            size = self._path.stat().st_size
        except OSError:
            return -1
        remaining = size - self._offset
        return max(0, remaining)

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self) -> RequestBody:
        return self

    def iter_bytes(self, chunk_size: int = _DEFAULT_CHUNK) -> Iterator[bytes]:
        remaining = self._count
        with self._path.open("rb") as stream:
            if self._offset:
                stream.seek(self._offset)
            while True:
                if remaining == 0:
                    return
                read = chunk_size if remaining == -1 else min(chunk_size, remaining)
                chunk = stream.read(read)
                if not chunk:
                    return
                yield chunk
                if remaining != -1:
                    remaining -= len(chunk)


def _from_file(
    cls: type[RequestBody],
    path: Path,
    media_type: MediaType | None = None,
    offset: int = 0,
    count: int = -1,
) -> RequestBody:
    """``RequestBody.from_file`` implementation, spliced onto the class.

    Defined here rather than in ``request_body.py`` to avoid a circular import.
    """
    del cls  # unused; behaves as a classmethod on RequestBody
    return FileRequestBody(path, media_type, offset, count)


# Attach as a classmethod so callers can write `RequestBody.from_file(...)`.
RequestBody.from_file = classmethod(_from_file)  # type: ignore[attr-defined]


__all__ = ["FileRequestBody"]
