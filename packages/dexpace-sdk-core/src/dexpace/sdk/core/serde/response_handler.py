# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Response handlers: turn a received `Response` into a typed result.

A `ResponseHandler` is the last hop of a call — it consumes the `Response` a
transport produced and returns the caller's result type (or raises). Two
handlers ship, both organised around one pure decision:

- `classify_status` is the single pure classification function. Given a
  response's status it returns a `ResponseDisposition` — ``SUCCESS`` for 2xx,
  ``ERROR`` for 4xx/5xx, ``UNEXPECTED`` for the 1xx/3xx in-between. It has no
  side effects: it never reads, closes, or raises. This is the *decision*.
- `DecodeBody` and `DecodeSuccess` are the *orchestration*. `DecodeBody`
  streams the body into the target type regardless of status, closing the
  response on every path. `DecodeSuccess` dispatches on `classify_status`:
  a 2xx delegates to `DecodeBody`, a 4xx/5xx raises the mapped typed error
  through a `StatusErrorMap`, and a 1xx/3xx raises a status-led error. Both
  share the one decode action (`DecodeSuccess` holds a `DecodeBody`), so the
  "decode a 2xx body" behaviour is defined once.

The call signature is positional-only — ``__call__(response, response_type, /)``
— so an alternate handler (a raw byte download, a HEAD request that decodes
nothing) can be swapped in wherever a `ResponseHandler` is expected.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, runtime_checkable

from ..errors import DeserializationError, HttpResponseError
from .codec import Codec
from .json_serde import JSON_SERDE
from .type_ref import TypeRef

if TYPE_CHECKING:
    from ..codegen.status_error_map import StatusErrorMap
    from ..http.response.response import Response
    from ..http.response.status import Status
    from .serde import Serde


class ResponseDisposition(enum.Enum):
    """How a response's status classifies for handling.

    The three-way outcome of `classify_status`, keyed off the status band:

    Attributes:
        SUCCESS: A 2xx response — decode the body into the target type.
        ERROR: A 4xx/5xx response — raise the mapped typed error.
        UNEXPECTED: A 1xx/3xx response — neither a success to decode nor a
            mapped error; a success-only handler raises a status-led error.
    """

    SUCCESS = enum.auto()
    ERROR = enum.auto()
    UNEXPECTED = enum.auto()


def classify_status(status: Status) -> ResponseDisposition:
    """Classify an HTTP status into a handling disposition.

    The single pure decision the response handlers are built over. It reads
    only the status band and has no side effects — it never touches the body,
    closes the response, or raises — so the decision can be tested in isolation
    from the handlers that act on it.

    Args:
        status: The response's HTTP status.

    Returns:
        `ResponseDisposition.SUCCESS` for a 2xx status,
        `ResponseDisposition.ERROR` for a 4xx/5xx status, and
        `ResponseDisposition.UNEXPECTED` for the 1xx/3xx in-between.
    """
    if status.is_success:
        return ResponseDisposition.SUCCESS
    if status.is_error:
        return ResponseDisposition.ERROR
    return ResponseDisposition.UNEXPECTED


@runtime_checkable
class ResponseHandler(Protocol):
    """A terminal step that turns a `Response` into the caller's result.

    Implementations receive the response and the requested result type and
    return whatever they produce (a decoded model, raw bytes, ``None`` for a
    HEAD request, ...) or raise. The call is positional-only so handlers stay
    interchangeable regardless of how they name their parameters, and the
    return type is deliberately `Any` so a non-decoding handler (a raw
    download) is just as valid a `ResponseHandler` as a decoding one.
    """

    def __call__(self, response: Response, response_type: type[Any] | TypeRef[Any], /) -> Any:
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
    """Return a readable name for a target type, for error messages.

    Args:
        response_type: The requested result type — a class, a subscripted
            generic, or a `TypeRef` wrapping one.

    Returns:
        The type's ``__name__`` when it has one, else its ``str()``. A
        `TypeRef` is unwrapped to its resolved witness first.
    """
    target = response_type.witness if isinstance(response_type, TypeRef) else response_type
    name = getattr(target, "__name__", None)
    return name if isinstance(name, str) else str(target)


class DecodeBody:
    """Decode the response body into the target type, whatever the status.

    Reads the body once, deserialises it through the configured `Serde`, and
    decodes the resulting document into the target type with the `Codec`. The
    response is consumed and closed on every path — a clean decode, a decode
    failure, and a missing body alike:

    - a missing body raises `DeserializationError` naming the target type;
    - a decode-shape failure (bad wire syntax, a document that does not fit the
      target) propagates as its `SerdeError`-family error, chained to its
      cause, never swallowed; and
    - a genuine `OSError` reading the body (a real I/O failure, not a
      decode-shape problem) propagates unwrapped.
    """

    __slots__ = ("_codec", "_serde")

    def __init__(self, *, serde: Serde | None = None, codec: Codec | None = None) -> None:
        """Configure the handler.

        Args:
            serde: Wire-format decoder for the raw body bytes. Defaults to JSON.
            codec: Document → typed-model codec. Defaults to a fresh `Codec`.
        """
        self._serde: Serde = serde if serde is not None else JSON_SERDE
        self._codec = codec if codec is not None else Codec()

    def __call__[T](self, response: Response, response_type: type[T] | TypeRef[T], /) -> T:
        """Decode ``response``'s body into ``response_type`` and close it.

        Args:
            response: The received response; closed before this returns or
                raises, on every path.
            response_type: The type to decode into.

        Returns:
            The decoded value.

        Raises:
            DeserializationError: If the response has no body (the message
                names ``response_type``), or if the body cannot be decoded.
            OSError: If reading the body fails with a genuine I/O error.
        """
        try:
            return self._decode(response, response_type)
        finally:
            response.close()

    def _decode[T](self, response: Response, response_type: type[T] | TypeRef[T]) -> T:
        """Read, deserialise, and decode the body (no closing — the caller does)."""
        body = response.body
        if body is None:
            raise DeserializationError(
                f"cannot decode a missing response body into {_type_name(response_type)}"
            )
        document = self._serde.deserializer.deserialize_bytes(body.bytes())
        return self._codec.decode(document, response_type)


class DecodeSuccess:
    """Decode only 2xx responses; raise a typed error for everything else.

    Dispatches on the pure `classify_status` decision:

    - ``SUCCESS`` (2xx): delegates to a `DecodeBody` — the body is decoded into
      the target type, and the response is closed on every path.
    - ``ERROR`` (4xx/5xx): raises the typed error the `StatusErrorMap` maps the
      status to, which carries a bounded, replayable copy of the error body.
    - ``UNEXPECTED`` (1xx/3xx): raises an `HttpResponseError` whose message
      leads with the status code, since a redirect or informational response is
      neither a decodable success nor a mapped error.

    The response is closed on every non-success path here, and on the success
    path by the delegated `DecodeBody`.
    """

    __slots__ = ("_decode_body", "_errors")

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
                responses. Its `raise_for` buffers the bounded error body.
            serde: Wire-format decoder for a 2xx body. Defaults to JSON.
            codec: Document → typed-model codec for a 2xx body. Defaults to a
                fresh `Codec`.
        """
        self._errors = errors
        self._decode_body = DecodeBody(serde=serde, codec=codec)

    def __call__[T](self, response: Response, response_type: type[T] | TypeRef[T], /) -> T:
        """Decode a 2xx ``response`` into ``response_type``, else raise.

        Args:
            response: The received response; closed on every path.
            response_type: The type a 2xx body is decoded into.

        Returns:
            The decoded value for a 2xx response.

        Raises:
            HttpResponseError: For a 4xx/5xx response (the mapped typed error,
                carrying the bounded body) or a 1xx/3xx response (a status-led
                error).
            DeserializationError: If a 2xx response has no body or cannot be
                decoded.
        """
        disposition = classify_status(response.status)
        if disposition is ResponseDisposition.SUCCESS:
            return self._decode_body(response, response_type)
        try:
            self._raise_non_success(response, disposition)
        finally:
            response.close()

    def _raise_non_success(self, response: Response, disposition: ResponseDisposition) -> NoReturn:
        """Raise the typed error for a non-success response.

        Args:
            response: The received non-success response.
            disposition: The `classify_status` decision — ``ERROR`` or
                ``UNEXPECTED`` (``SUCCESS`` is handled before this is called).

        Raises:
            HttpResponseError: The mapped error for a 4xx/5xx status (via
                `StatusErrorMap.raise_for`), or a status-led error for a
                1xx/3xx status.
        """
        if disposition is ResponseDisposition.ERROR:
            self._errors.raise_for(response)
        raise _unexpected_status_error(response)


def _unexpected_status_error(response: Response) -> HttpResponseError[Any]:
    """Build a status-led error for a 1xx/3xx response a success handler rejects.

    Args:
        response: The received informational/redirect response.

    Returns:
        An `HttpResponseError` carrying ``response``, whose message leads with
        the numeric status code.
    """
    status = response.status
    return HttpResponseError(
        f"{int(status)} {status.name}: unexpected response status; a success handler "
        f"decodes only 2xx responses and raises the mapped error for 4xx/5xx",
        response=response,
    )


__all__ = [
    "DecodeBody",
    "DecodeSuccess",
    "ResponseDisposition",
    "ResponseHandler",
    "classify_status",
]
