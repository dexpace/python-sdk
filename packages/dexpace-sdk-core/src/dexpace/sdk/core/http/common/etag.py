# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""RFC 7232 entity-tag value object."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class ETag:
    """An HTTP entity tag (RFC 7232 §2.3).

    The opaque ``value`` does NOT include the surrounding double quotes;
    ``__str__`` re-adds them on serialization. An empty ``value`` is
    rejected by ``parse`` — the wire form ``""`` carries no useful
    validator information.

    RFC 7232 distinguishes two validator strengths:

    - **Strong validators** (``weak=False``, wire form ``"abc"``) signal
      that two representations with the same tag are byte-identical. They
      may be used for any conditional request, including range requests
      (``If-Match`` / ``If-Range``).
    - **Weak validators** (``weak=True``, wire form ``W/"abc"``) signal
      only semantic equivalence — the resource is "good enough" the same
      but may differ byte-for-byte (e.g. compressed vs uncompressed
      payloads, or content negotiated on insignificant headers). Weak
      tags are valid for ``If-None-Match`` cache revalidation but MUST
      NOT be used with ``If-Range`` for partial-content requests.

    Use ``matches_strong`` for strong comparison (both sides must be
    strong) and ``matches_weak`` for weak comparison (the ``weak`` flag
    is ignored on either side). See RFC 7232 §2.3.2 for the comparison
    rules.

    Attributes:
        value: The opaque entity-tag bytes (no quotes). Must be non-empty
            when constructed via ``parse``.
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
            ValueError: If ``raw`` is not a valid quoted entity-tag, if the
                quoted body is empty, or if the body contains a double-quote
                or control character (forbidden in the opaque tag by
                RFC 7232 §2.3).
        """
        text = raw.strip()
        weak = False
        if text.startswith("W/"):
            weak = True
            text = text[2:]
        if len(text) < 2 or text[0] != '"' or text[-1] != '"':
            raise ValueError(f"Invalid ETag: {raw!r}")
        value = text[1:-1]
        if not value:
            raise ValueError(f"Invalid ETag: empty quoted body in {raw!r}")
        if '"' in value:
            raise ValueError(f"Invalid ETag: embedded double-quote in {raw!r}")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError(f"Invalid ETag: control character in {raw!r}")
        return cls(value=value, weak=weak)


__all__ = ["ETag"]
