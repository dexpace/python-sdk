# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous platform executor a generated SDK facade delegates to.

`ServiceCore` is the sync half of the executor pair. A generated client method
does not talk to the pipeline directly; it declares an `Operation`, packs its
arguments into an `OperationInput`, and calls one of this executor's four
entry points:

- `execute` / `execute_request` — a single request/response exchange, decoded
  into the caller's `response_type` by the default `DecodeSuccess` handler.
- `paginate` — returns the certified `Paginator`, so the wire-exact splice and
  close-ledger guarantees carry through unchanged.
- `events` — returns the certified SSE connection with a typed mapper applied,
  leaving the core SSE layer free of any service-specific convention.

Every non-awaiting decision — request binding, per-call options packing, and
the raw-vs-handler result routing — lives in the shared pure planners under
`_pure/`, which `AsyncServiceCore` imports too; only the awaiting shell differs
between the two. The executor is an ownership-aware context manager: closing it
closes a transport it constructed a pipeline around, but never a caller-supplied
(BYO) pipeline.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, overload

from ..http.response.response import Response
from ..pipeline.defaults import default_pipeline
from ..serde import JSON_SERDE, DecodeSuccess
from ._pure.options import plan_options
from ._pure.requests import plan_request
from ._pure.results import plan_result
from .status_error_map import StatusErrorMap

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..client.http_client import HttpClient
    from ..http.common import Url
    from ..http.context.dispatch_context import DispatchContext
    from ..http.request.request import Request
    from ..http.sse.typed import SseMapper, TypedSseConnection
    from ..pagination import PaginationStrategy, Paginator
    from ..pipeline.pipeline import Pipeline
    from ..pipeline.policy import Policy
    from ..serde import ResponseHandler, Serde, TypeRef
    from .operation import Operation, OperationInput

__all__ = ["ServiceCore"]

#: A resolved response-type witness accepted by `execute` / `execute_request`.
type _Witness[T] = type[T] | TypeRef[T]


class ServiceCore:
    """Synchronous executor over the shared pure planners.

    Construct from exactly one of a transport or a BYO pipeline:

    - ``ServiceCore(base_url=..., transport=client)`` builds the canonical
      `default_pipeline` around ``client`` and *owns* it — closing the executor
      closes ``client``.
    - ``ServiceCore(base_url=..., pipeline=my_pipeline)`` borrows ``my_pipeline``
      and never closes it (bake in auth or a custom stack this way).
    """

    __slots__ = (
        "_base_url",
        "_closed",
        "_default_handler",
        "_dispatch_factory",
        "_errors",
        "_owned_transport",
        "_pipeline",
        "_serde",
    )

    def __init__(
        self,
        *,
        base_url: str | Url,
        pipeline: Pipeline | None = None,
        transport: HttpClient | None = None,
        errors: StatusErrorMap | None = None,
        serde: Serde | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        """Wire the executor to a pipeline (BYO) or a transport it builds one around.

        Args:
            base_url: Absolute base URL every operation's path joins onto.
            pipeline: A caller-supplied `Pipeline`. Borrowed, never closed by
                this executor. Mutually exclusive with ``transport``.
            transport: An `HttpClient` to build the `default_pipeline` around.
                Owned — closing the executor closes it. Mutually exclusive with
                ``pipeline``.
            errors: Status → typed-error mapping for the default handler.
                Defaults to an empty map (a bare `HttpResponseError` per error
                status).
            serde: Wire-format serde for request/response bodies. Defaults to
                JSON.
            dispatch_factory: Builds the per-call dispatch context. Defaults to
                `DispatchContext.noop`.

        Raises:
            ValueError: If neither or both of ``pipeline`` and ``transport`` are
                given.
        """
        if (pipeline is None) == (transport is None):
            raise ValueError("provide exactly one of pipeline= or transport=")
        from ..http.context.dispatch_context import DispatchContext

        self._base_url = base_url
        self._serde: Serde = serde if serde is not None else JSON_SERDE
        self._errors = errors if errors is not None else StatusErrorMap()
        self._dispatch_factory = (
            dispatch_factory if dispatch_factory is not None else DispatchContext.noop
        )
        self._default_handler: ResponseHandler = DecodeSuccess(self._errors, serde=self._serde)
        self._closed = False
        if pipeline is not None:
            self._pipeline = pipeline
            self._owned_transport: HttpClient | None = None
        else:
            assert transport is not None  # narrowed by the exclusivity guard above
            self._pipeline = default_pipeline(transport).build()
            self._owned_transport = transport

    # -- execute --------------------------------------------------------------

    @overload
    def execute[T](
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: _Witness[T],
        handler: ResponseHandler | None = ...,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> T: ...

    @overload
    def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: None = ...,
        handler: None = ...,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> Response: ...

    @overload
    def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: None = ...,
        handler: ResponseHandler,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> Any: ...

    def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: _Witness[Any] | None = None,
        handler: ResponseHandler | None = None,
        auth: Policy | None = None,
        **call_options: Any,
    ) -> Any:
        """Run one operation and return its decoded result.

        Args:
            op: The static operation description.
            input: The per-call values (path params, query, headers, body).
            response_type: The type to decode a 2xx body into. When ``None``
                (and no ``handler``), the raw `Response` is returned for the
                caller to consume and close.
            handler: An explicit response handler overriding the default
                `DecodeSuccess`. Receives the response and the witness.
            auth: A per-call auth override, threaded through the options mapping.
            **call_options: Per-call overrides forwarded to the pipeline's
                policies via ``ctx.options``.

        Returns:
            The decoded value for a given ``response_type``, the raw `Response`
            when neither ``response_type`` nor ``handler`` is given, or the
            handler's result.
        """
        request = plan_request(op, input, self._base_url, self._serde)
        return self.execute_request(
            request, response_type=response_type, handler=handler, auth=auth, **call_options
        )

    # -- execute_request ------------------------------------------------------

    @overload
    def execute_request[T](
        self,
        request: Request,
        *,
        response_type: _Witness[T],
        handler: ResponseHandler | None = ...,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> T: ...

    @overload
    def execute_request(
        self,
        request: Request,
        *,
        response_type: None = ...,
        handler: None = ...,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> Response: ...

    @overload
    def execute_request(
        self,
        request: Request,
        *,
        response_type: None = ...,
        handler: ResponseHandler,
        auth: Policy | None = ...,
        **call_options: Any,
    ) -> Any: ...

    def execute_request(
        self,
        request: Request,
        *,
        response_type: _Witness[Any] | None = None,
        handler: ResponseHandler | None = None,
        auth: Policy | None = None,
        **call_options: Any,
    ) -> Any:
        """Run a pre-built `Request` through the pipeline — the raw-call seam.

        Args:
            request: A fully-formed request to send as-is.
            response_type: The type to decode a 2xx body into, or ``None`` for
                the raw `Response`.
            handler: An explicit response handler overriding the default.
            auth: A per-call auth override, threaded through the options mapping.
            **call_options: Per-call overrides forwarded to the pipeline.

        Returns:
            The decoded value, the raw `Response`, or the handler's result — as
            for `execute`.
        """
        options = plan_options(auth, call_options)
        response = self._pipeline.run(request, self._dispatch_factory(), **options)
        return self._handle(response, response_type, handler)

    def _handle(
        self,
        response: Response,
        response_type: _Witness[Any] | None,
        handler: ResponseHandler | None,
    ) -> Any:
        """Apply the planned result handling to ``response`` (awaiting-free)."""
        plan = plan_result(handler, self._default_handler, response_type, Response)
        if plan is None:
            return response
        chosen, witness = plan
        return chosen(response, witness)

    # -- paginate / events ----------------------------------------------------

    def paginate[T](
        self,
        op: Operation,
        input: OperationInput,
        strategy: PaginationStrategy[T],
        *,
        max_pages: int | None = None,
        auth: Policy | None = None,
        **call_options: Any,
    ) -> Paginator[T]:
        """Return a `Paginator` walking the operation's paged sequence.

        The returned paginator is the certified `Paginator`; it is never
        re-implemented here, so its wire-exact next-request splice and
        close-ledger guarantees carry through. Each page fetch runs through this
        executor's pipeline with the packed per-call options.

        Args:
            op: The static operation description for the first page.
            input: The per-call values for the first page.
            strategy: The `PaginationStrategy` that parses each response into a
                page and derives the next request.
            max_pages: Optional cap on pages fetched. ``None`` is unbounded.
            auth: A per-call auth override, threaded through the options mapping.
            **call_options: Per-call overrides forwarded to every page fetch.

        Returns:
            A `Paginator` over ``strategy``'s element type.
        """
        from ..pagination import Paginator

        request = plan_request(op, input, self._base_url, self._serde)
        return Paginator(
            self._page_sender(auth, call_options), strategy, request, max_pages=max_pages
        )

    def events[T](
        self,
        op: Operation,
        input: OperationInput,
        mapper: SseMapper[T],
        *,
        auth: Policy | None = None,
        **call_options: Any,
    ) -> TypedSseConnection[T]:
        """Return a typed SSE connection over the operation's event stream.

        The connection is the certified reconnecting `SseConnection` wrapped in
        the certified `TypedSseConnection`, with ``mapper`` applied. The core
        SSE layer stays free of any sentinel / serde convention — what a data
        payload means (a ``[DONE]`` marker, a JSON document) is entirely the
        mapper's decision.

        Args:
            op: The static operation description opening the stream.
            input: The per-call values for the opening request.
            mapper: Maps ``(event_name, data)`` to a typed value or an
                `SseSignal` (skip / done).
            auth: A per-call auth override, threaded through the options mapping.
            **call_options: Per-call overrides forwarded to every (re)connection.

        Returns:
            A `TypedSseConnection` over ``mapper``'s value type.
        """
        from ..http.sse.connection import SseConnection
        from ..http.sse.typed import TypedSseConnection

        request = plan_request(op, input, self._base_url, self._serde)
        connection = SseConnection(self._page_sender(auth, call_options), request)
        return TypedSseConnection(connection, mapper)

    def _page_sender(
        self, auth: Policy | None, call_options: Mapping[str, Any]
    ) -> Callable[[Request], Response]:
        """Build a send-callable that threads per-call options into each fetch.

        Paginators and SSE connections accept a bare ``Request -> Response``
        callable. Passing one (rather than the pipeline itself) is how the
        packed per-call options reach every page / reconnection, since their
        built-in pipeline path runs ``pipeline.run`` without options.
        """
        options = plan_options(auth, call_options)

        def send(request: Request) -> Response:
            return self._pipeline.run(request, self._dispatch_factory(), **options)

        return send

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close an owned transport; leave a BYO pipeline untouched. Idempotent.

        A transport this executor built its pipeline around is closed (its own
        ownership-aware close then decides whether to tear down its underlying
        resource). A caller-supplied pipeline is never closed here. A second
        call is a no-op.
        """
        if self._closed:
            return
        self._closed = True
        transport = self._owned_transport
        if transport is None:
            return
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
