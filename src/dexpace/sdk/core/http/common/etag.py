"""RFC 7232 entity-tag value object."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class ETag:
    """An HTTP entity tag (RFC 7232 §2.3).

    The opaque ``value`` does NOT include the surrounding double quotes;
    ``__str__`` re-adds them on serialization. The ``weak`` flag determines
    whether the tag is emitted with the ``W/`` prefix and governs whether
    two ETags are considered weakly or strongly equivalent.

    Attributes:
        value: The opaque entity-tag bytes (no quotes).
        weak: When ``True``, emit and compare as a weak validator.
    """

    value: str
    weak: bool = False

    def __str__(self) -> str:
        quoted = f'"{self.value}"'
        return f"W/{quoted}" if self.weak else quoted

    def matches_strong(self, other: ETag) -> bool:
        """Compare two ETags using RFC 7232 strong-comparison rules.

        Args:
            other: The ETag to compare against.

        Returns:
            ``True`` when both values are equal and neither tag is weak.
        """
        return not self.weak and not other.weak and self.value == other.value

    def matches_weak(self, other: ETag) -> bool:
        """Compare two ETags using RFC 7232 weak-comparison rules.

        Args:
            other: The ETag to compare against.

        Returns:
            ``True`` when the values are equal, regardless of either tag's
            ``weak`` flag.
        """
        return self.value == other.value

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse the wire form of an entity tag.

        Args:
            raw: A quoted string (e.g. ``"abc"``) or weak form (``W/"abc"``).

        Returns:
            The parsed ETag.

        Raises:
            ValueError: If ``raw`` is not a valid quoted entity-tag.
        """
        text = raw.strip()
        weak = False
        if text.startswith("W/"):
            weak = True
            text = text[2:]
        if len(text) < 2 or text[0] != '"' or text[-1] != '"':
            raise ValueError(f"Invalid ETag: {raw!r}")
        return cls(value=text[1:-1], weak=weak)


__all__ = ["ETag"]
