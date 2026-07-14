# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Runtime binders the generated facades reference.

A generated SDK ships a small hand-authored shim next to its facades. This is
that shim for the petstore canary:

- `json_body` encodes a typed model into a JSON `RequestBody`, folding the
  `Tristate` fields of a merge-patch body (absent keys are dropped, ``NULL``
  becomes ``null``, `Present` values are written).
- `PET_PAGE_STRATEGY` is the pagination strategy for ``list_pets`` — a decoding
  wrapper over the certified `CursorStrategy` that reconstructs each page's raw
  item documents into typed `Pet` values.
- `PET_EVENT_MAPPER` is the SSE mapper for ``watch_pets`` — it stops on a
  ``[DONE]`` sentinel and decodes every other frame's JSON into a `PetEvent`.

The facades hold no logic of their own; they name these binders and delegate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.http.request import RequestBody
from dexpace.sdk.core.http.sse.typed import SseMapper, SseSignal
from dexpace.sdk.core.pagination import CursorStrategy, HasHeaders, Page, PaginationStrategy
from dexpace.sdk.core.serde import JSON_SERDE, Codec

from .models import Pet, PetEvent

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request import Request

__all__ = ["PET_EVENT_MAPPER", "PET_PAGE_STRATEGY", "json_body"]

#: The one shared codec instance the binders reuse. `Codec` is stateless and
#: thread-safe, so a single instance serves every model.
_CODEC: Codec = Codec()


def json_body(model: object) -> RequestBody:
    """Encode a typed model into a JSON `RequestBody`.

    Args:
        model: A dataclass model (e.g. a `PetPatch`). Its `Tristate` fields are
            folded by the codec: absent keys are omitted, ``NULL`` is written as
            ``null``, and `Present` values are written verbatim.

    Returns:
        A replayable ``application/json`` request body carrying the encoded
        document.
    """
    document = _CODEC.encode(model)
    data = JSON_SERDE.serializer.serialize_to_bytes(document)
    return RequestBody.from_bytes(data, common_media_types.APPLICATION_JSON)


@dataclass(frozen=True, slots=True)
class _PetPageStrategy:
    """Decoding pagination strategy: `CursorStrategy` plus per-item `Pet` decode.

    The certified `CursorStrategy` reads the item list and cursor out of the
    response body but leaves items as raw documents. This wrapper delegates the
    page mechanics to it, then reconstructs each raw item into a typed `Pet`, so
    the paginator yields `Pet` values while the wire-splice and close-ledger
    guarantees stay with the certified strategy.
    """

    inner: CursorStrategy[object]

    def parse(self, response: HasHeaders, payload: object, template_request: Request) -> Page[Pet]:
        """Parse one response into a page of typed `Pet` items.

        Args:
            response: The response, for header inspection by the inner strategy.
            payload: The decoded response body (a mapping with a ``data`` list).
            template_request: The request that produced ``response``.

        Returns:
            A page whose items are decoded `Pet` values and whose next-request
            splice is exactly the one the inner strategy derived.
        """
        page = self.inner.parse(response, payload, template_request)
        pets = [_CODEC.decode(item, Pet) for item in page.items]
        return Page(
            items=pets,
            next_request=page.next_request,
            prev_request=page.prev_request,
            raw=page.raw,
        )


#: Pagination strategy for ``list_pets``: cursor pagination over a ``data`` array
#: with a ``next_cursor`` continuation, decoding each item into a `Pet`.
PET_PAGE_STRATEGY: PaginationStrategy[Pet] = _PetPageStrategy(
    CursorStrategy(items_field="data", cursor_response_field="next_cursor", cursor_param="cursor")
)


def _pet_event_mapper(event: str, data: str) -> PetEvent | SseSignal:
    """Map one SSE frame to a `PetEvent`, stopping on the ``[DONE]`` sentinel.

    Args:
        event: The SSE event name (unused; every frame is a pet event).
        data: The frame's data payload — a ``[DONE]`` marker or a JSON document.

    Returns:
        `SseSignal.DONE` for the sentinel, otherwise the decoded `PetEvent`.
    """
    if data == "[DONE]":
        return SseSignal.DONE
    document = JSON_SERDE.deserializer.deserialize_bytes(data.encode())
    return _CODEC.decode(document, PetEvent)


#: SSE mapper for ``watch_pets``: decode each frame into a `PetEvent`.
PET_EVENT_MAPPER: SseMapper[PetEvent] = _pet_event_mapper
