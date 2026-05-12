"""RFC 7233 byte-range value object for `Range` and `Content-Range` headers."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HttpRange:
    """A single byte range.

    Two shapes are supported:

    - Bounded: ``HttpRange(N, M)`` → ``bytes=N-(N+M-1)``.
    - Open-ended: ``HttpRange(N)`` → ``bytes=N-`` ("from N to the end").

    Last-N-bytes ranges (``bytes=-N``) are not representable directly because
    ``start`` is constrained to be non-negative. Use ``HttpRange.suffix(N)``
    to construct the suffix variant, which is emitted by a sibling type.

    HTTP allows multipart ranges (``bytes=0-99,200-299``); model those as a
    sequence of ``HttpRange`` and join via ``HttpRange.format_many`` when
    emitting a header value.

    Attributes:
        start: First byte index (inclusive). Non-negative.
        count: Number of bytes to fetch, or ``None`` for open-ended.

    Raises:
        ValueError: If ``start`` is negative, or ``count`` is set but
            non-positive.
    """

    start: int = 0
    count: int | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"start must be non-negative, got {self.start}")
        if self.count is not None and self.count <= 0:
            raise ValueError(f"count must be positive when set, got {self.count}")

    @property
    def end(self) -> int | None:
        """Inclusive end byte index, or ``None`` for open-ended ranges."""
        if self.count is None:
            return None
        return self.start + self.count - 1

    def format(self) -> str:
        """Format as the value portion of a ``Range`` header (no ``bytes=`` prefix)."""
        end = self.end
        if end is None:
            return f"{self.start}-"
        return f"{self.start}-{end}"

    def to_header_value(self) -> str:
        """Format as a complete ``Range`` header value, including ``bytes=``."""
        return f"bytes={self.format()}"

    @classmethod
    def suffix(cls, count: int) -> _SuffixHttpRange:
        """Construct a last-N-bytes range (``bytes=-N``).

        Args:
            count: Number of trailing bytes to request. Must be positive.

        Returns:
            A ``_SuffixHttpRange`` that serialises to ``bytes=-N``.

        Raises:
            ValueError: If ``count`` is non-positive.
        """
        if count <= 0:
            raise ValueError(f"suffix count must be positive, got {count}")
        return _SuffixHttpRange(count=count)

    @classmethod
    def format_many(cls, ranges: Sequence[HttpRange]) -> str:
        """Format multiple ranges as a multipart ``Range`` header value.

        Args:
            ranges: The ranges to combine, in the order they appear on the wire.

        Returns:
            A complete header value, e.g. ``bytes=0-99,200-299``.

        Raises:
            ValueError: If ``ranges`` is empty.
        """
        if not ranges:
            raise ValueError("ranges must not be empty")
        joined = ",".join(r.format() for r in ranges)
        return f"bytes={joined}"


@dataclass(frozen=True, slots=True)
class _SuffixHttpRange:
    """``bytes=-N`` — the last ``count`` bytes of the resource."""

    count: int

    def format(self) -> str:
        return f"-{self.count}"

    def to_header_value(self) -> str:
        return f"bytes={self.format()}"


__all__ = ["HttpRange"]
