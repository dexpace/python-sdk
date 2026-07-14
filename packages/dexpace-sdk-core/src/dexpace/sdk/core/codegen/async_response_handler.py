# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async response handler — the async twin of serde's `DecodeSuccess`.

The serde layer (PY-M5-06) ships a sync `DecodeSuccess` that turns a `Response`
into a typed result, dispatching on the pure `classify_status` decision. It is
sync-only: it reads the body and closes the response without awaiting, so it
cannot drive an `AsyncResponse`. The async executor needs the same behaviour
over the awaiting surface, so `AsyncDecodeSuccess` mirrors it here.

Only the awaiting shell differs. The *decision* — SUCCESS decodes a 2xx body,
ERROR raises the `StatusErrorMap`-mapped typed error, UNEXPECTED raises a
status-led error — is the shared pure `classify_status`, and the *decode
action* reuses serde's own primitives (the configured `Serde` deserializer and
`Codec`). No classification or decode logic is restated here; only
``await response.body.bytes()`` and ``await response.close()`` replace their
sync counterparts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn, Protocol, runtime_checkable

from ..errors import DeserializationError, HttpResponseError
from ..serde import JSON_SERDE, Codec, ResponseDisposition, TypeRef, classify_status

if TYPE_CHECKING:
    from ..http.response.async_response import AsyncResponse
    from ..serde import Serde
    from .status_error_map import StatusErrorMap

__all__ = ["AsyncDecodeSuccess", "AsyncResponseHandler"]


@runtime_checkable
class AsyncResponseHandler(Protocol):
    """Async twin of serde's `ResponseHandler` Protocol.

    A terminal step that turns an `AsyncResponse` into the caller's result,
    awaiting the body it consumes. The call is positional-only so handlers stay
    interchangeable regardless of how they name their parameters, and the
    awaited result is `Any` so a non-decoding handler (a raw download) is just
    as valid a handler as a decoding one.
    """

    async def __call__(
        self, response: AsyncResponse, response_type: type[Any] | TypeRef[Any], /
    ) -> Any:
        """Handle ``response``, producing the caller's result.

        Args:
            response: The received response. The handler owns closing it.
            response_type: The requested result type — a class, a subscripted
                generic (``list[Pet]``), or a `TypeRef`. A handler that decodes
                nothing may ignore it.

        Returns:
            The handler's result.
        """
        ...


def _type_name(response_type: object) -> str:
    """Return a readable name for a target type, for error messages."""
    target = response_type.witness if isinstance(response_type, TypeRef) else response_type
    name = getattr(target, "__name__", None)
    return name if isinstance(name, str) else str(target)


class AsyncDecodeSuccess:
    """Decode only 2xx async responses; raise a typed error for everything else.

    Async twin of serde's `DecodeSuccess`. Dispatches on the shared pure
    `classify_status` decision:

    - ``SUCCESS`` (2xx): the body is read and decoded into the target type
      through the configured `Serde` and `Codec`; the response is closed on
      every path.
    - ``ERROR`` (4xx/5xx): raises the typed error the `StatusErrorMap` maps the
      status to. (The error's bounded body copy is only captured for a sync
      response body; an async body yields an empty capture — a pre-existing
      limitation of the errors layer, not introduced here.)
    - ``UNEXPECTED`` (1xx/3xx): raises an `HttpResponseError` whose message
      leads with the status code.
    """

    __slots__ = ("_codec", "_errors", "_serde")

    def __init__(
        self,
        errors: StatusErrorMap,
        *,
        serde: Serde | None = None,
        codec: Codec | None = None,
    ) -> None:
        """Configure the handler.

        Args:
            errors: Status → typed-error mapping consulted for 4xx/5xx
                responses.
            serde: Wire-format decoder for a 2xx body. Defaults to JSON.
            codec: Document → typed-model codec for a 2xx body. Defaults to a
                fresh `Codec`.
        """
        self._errors = errors
        self._serde: Serde = serde if serde is not None else JSON_SERDE
        self._codec = codec if codec is not None else Codec()

    async def __call__[T](
        self, response: AsyncResponse, response_type: type[T] | TypeRef[T], /
    ) -> T:
        """Decode a 2xx ``response`` into ``response_type``, else raise.

        Args:
            response: The received response; closed on every path.
            response_type: The type a 2xx body is decoded into.

        Returns:
            The decoded value for a 2xx response.

        Raises:
            HttpResponseError: For a 4xx/5xx response (the mapped typed error)
                or a 1xx/3xx response (a status-led error).
            DeserializationError: If a 2xx response has no body or cannot be
                decoded.
        """
        disposition = classify_status(response.status)
        if disposition is ResponseDisposition.SUCCESS:
            try:
                return await self._decode(response, response_type)
            finally:
                await response.close()
        try:
            self._raise_non_success(response, disposition)
        finally:
            await response.close()

    async def _decode[T](self, response: AsyncResponse, response_type: type[T] | TypeRef[T]) -> T:
        """Read, deserialise, and decode the body (no closing — the caller does)."""
        body = response.body
        if body is None:
            raise DeserializationError(
                f"cannot decode a missing response body into {_type_name(response_type)}"
            )
        document = self._serde.deserializer.deserialize_bytes(await body.bytes())
        return self._codec.decode(document, response_type)

    def _raise_non_success(
        self, response: AsyncResponse, disposition: ResponseDisposition
    ) -> NoReturn:
        """Raise the typed error for a non-success response.

        Uses `StatusErrorMap.for_status` — the non-raising, non-buffering
        lookup — rather than the sync-typed `StatusErrorMap.raise_for`, which
        expects a `Response`. The mapped error is constructed against the
        `AsyncResponse` directly; the errors layer does not synchronously drain
        an async body, so no bounded body copy is captured for an async error
        (a pre-existing limitation of the errors layer, not introduced here).

        Args:
            response: The received non-success response.
            disposition: The `classify_status` decision — ``ERROR`` or
                ``UNEXPECTED`` (``SUCCESS`` is handled before this is called).

        Raises:
            HttpResponseError: The mapped error for a 4xx/5xx status, or a
                status-led error for a 1xx/3xx status.
        """
        if disposition is ResponseDisposition.ERROR:
            error_class = self._errors.for_status(response.status)
            if error_class is not None:
                raise error_class(response=response)
        raise _unexpected_status_error(response)


def _unexpected_status_error(response: AsyncResponse) -> HttpResponseError[Any]:
    """Build a status-led error for a 1xx/3xx response a success handler rejects."""
    status = response.status
    return HttpResponseError(
        f"{int(status)} {status.name}: unexpected response status; a success handler "
        f"decodes only 2xx responses and raises the mapped error for 4xx/5xx",
        response=response,
    )
