"""Conditional-request preconditions for `If-Match` / `If-None-Match` family."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING

from .etag import ETag
from .http_header_name import (
    IF_MATCH,
    IF_MODIFIED_SINCE,
    IF_NONE_MATCH,
    IF_UNMODIFIED_SINCE,
)

if TYPE_CHECKING:
    from ..request.request import Request


@dataclass(frozen=True, slots=True)
class RequestConditions:
    """Bundle of RFC 7232 conditional-request preconditions.

    Any field may be ``None`` to omit; ``if_match`` / ``if_none_match`` accept
    a sequence of ``ETag`` (the wildcard ``*`` is expressed by passing
    ``ETag(value="*", weak=False)`` — the serialiser detects and emits it as
    a bare ``*``). Apply to an outgoing request via ``apply_to``.

    Attributes:
        if_match: Tags for the ``If-Match`` precondition.
        if_none_match: Tags for the ``If-None-Match`` precondition.
        if_modified_since: Lower bound for ``Last-Modified``.
        if_unmodified_since: Upper bound for ``Last-Modified``.
    """

    if_match: Sequence[ETag] | None = None
    if_none_match: Sequence[ETag] | None = None
    if_modified_since: datetime | None = None
    if_unmodified_since: datetime | None = None

    def apply_to(self, request: Request) -> Request:
        """Return ``request`` with conditional headers applied.

        Existing values for the conditional headers are replaced.

        Args:
            request: The outgoing request to derive from.

        Returns:
            A new request with the relevant conditional headers set.
        """
        result = request
        if self.if_match is not None:
            result = result.with_header(IF_MATCH, _format_etags(self.if_match))
        if self.if_none_match is not None:
            result = result.with_header(
                IF_NONE_MATCH, _format_etags(self.if_none_match)
            )
        if self.if_modified_since is not None:
            result = result.with_header(
                IF_MODIFIED_SINCE, _format_http_date(self.if_modified_since)
            )
        if self.if_unmodified_since is not None:
            result = result.with_header(
                IF_UNMODIFIED_SINCE, _format_http_date(self.if_unmodified_since)
            )
        return result


def _format_etags(tags: Sequence[ETag]) -> str:
    if not tags:
        raise ValueError("At least one ETag required")
    # The bare ``*`` wildcard form is special-cased: RFC 7232 §3.1 specifies it
    # MUST NOT be quoted, so we emit it as ``*`` rather than the regular
    # ``"*"`` form ``ETag.__str__`` would produce.
    if len(tags) == 1 and tags[0].value == "*" and not tags[0].weak:
        return "*"
    return ", ".join(str(tag) for tag in tags)


def _format_http_date(value: datetime) -> str:
    # email.utils.format_datetime produces the IMF-fixdate form (RFC 7231
    # §7.1.1.1) when given a timezone-aware datetime. Force UTC if naive —
    # callers passing local-time naive datetimes is a common bug source and
    # silently misinterpreting them produces wrong cache behaviour.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return format_datetime(value, usegmt=True)


__all__ = ["RequestConditions"]
