# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""RFC 7233 byte-range value object for `Range` and `Content-Range` headers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HttpRange:
    """A single byte range.

    Three shapes are supported:

    - Bounded: ``HttpRange(N, M)`` → ``bytes=N-(N+M-1)``.
    - Open-ended: ``HttpRange(N)`` → ``bytes=N-`` ("from N to the end").
    - Suffix: ``HttpRange.suffix(N)`` → ``bytes=-N`` (the last N bytes).

    A suffix range sets ``is_suffix`` and reuses ``count`` as the number of
    trailing bytes; ``start`` is ignored for that shape. Because a suffix
    range is an ordinary ``HttpRange``, it can be mixed freely into a
    ``Sequence[HttpRange]`` passed to ``format_many``. Prefer the
    ``HttpRange.suffix`` factory over constructing one with ``is_suffix=True``
    directly.

    HTTP allows multipart ranges (``bytes=0-99,200-299``); model those as a
    sequence of ``HttpRange`` and join via ``HttpRange.format_many`` when
    emitting a header value.

    Attributes:
        start: First byte index (inclusive). Non-negative. Ignored when
            ``is_suffix`` is set.
        count: Number of bytes to fetch, or ``None`` for open-ended. For a
            suffix range, the number of trailing bytes (required, positive).
        is_suffix: When ``True``, serialise as ``bytes=-count`` instead of a
            ``start``-anchored range.

    Raises:
        ValueError: If ``start`` is negative; if ``count`` is set but
            non-positive; or if ``is_suffix`` is set without a positive
            ``count``.
    """

    start: int = 0
    count: int | None = None
    is_suffix: bool = False

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"start must be non-negative, got {self.start}")
        if self.count is not None and self.count <= 0:
            raise ValueError(f"count must be positive when set, got {self.count}")
        if self.is_suffix and self.count is None:
            raise ValueError("suffix range requires a positive count")

    @property
    def end(self) -> int | None:
        """Inclusive end byte index, or ``None`` for open-ended or suffix ranges."""
        if self.count is None or self.is_suffix:
            return None
        return self.start + self.count - 1

    def format(self) -> str:
        """Format as the value portion of a ``Range`` header (no ``bytes=`` prefix)."""
        if self.is_suffix:
            return f"-{self.count}"
        end = self.end
        if end is None:
            return f"{self.start}-"
        return f"{self.start}-{end}"

    def to_header_value(self) -> str:
        """Format as a complete ``Range`` header value, including ``bytes=``."""
        return f"bytes={self.format()}"

    @classmethod
    def suffix(cls, count: int) -> HttpRange:
        """Construct a last-N-bytes range (``bytes=-N``).

        Args:
            count: Number of trailing bytes to request. Must be positive.

        Returns:
            An ``HttpRange`` with ``is_suffix`` set that serialises to
            ``bytes=-N``.

        Raises:
            ValueError: If ``count`` is non-positive.
        """
        if count <= 0:
            raise ValueError(f"suffix count must be positive, got {count}")
        return cls(count=count, is_suffix=True)

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


__all__ = ["HttpRange"]
