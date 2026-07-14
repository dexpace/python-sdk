# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Asynchronous platform executor a generated async SDK facade delegates to.

`AsyncServiceCore` is the async twin of `ServiceCore`. It exposes the same four
entry points — `execute` / `execute_request` / `paginate` / `events` — and
shares every non-awaiting decision with the sync executor through the pure
planners under `_pure/` (request binding, options packing, result routing).
Only the awaiting shell differs: the pipeline run is awaited, the response
handler is awaited, and lifecycle uses `aclose` / the async context-manager
protocol.

The default handler is `AsyncDecodeSuccess`, the async twin of serde's
`DecodeSuccess` — serde ships only the sync handler, and its layer is frozen,
so the async orchestration over the same shared `classify_status` decision and
serde decode primitives lives alongside this executor. `paginate` returns the
certified `AsyncPaginator` and `events` the certified `AsyncSseConnection`
wrapped in `AsyncTypedSseConnection`; neither is re-implemented here.
"""

from __future__ import annotations

from collections.abc import Awaitable
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, overload

from ..http.response.async_response import AsyncResponse
from ..pipeline.async_policy import AsyncPolicy
from ..pipeline.defaults import default_async_pipeline
from ..serde import JSON_SERDE
from ._pure.options import plan_options
from ._pure.requests import plan_request
from ._pure.results import plan_result
from .async_response_handler import AsyncDecodeSuccess
from .auth import AuthRegistry, resolve_auth
from .status_error_map import StatusErrorMap

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..client.async_http_client import AsyncHttpClient
    from ..http.common import Url
    from ..http.context.dispatch_context import DispatchContext
    from ..http.request.request import Request
    from ..http.sse.typed import AsyncTypedSseConnection, SseMapper
    from ..pagination import AsyncPaginator, PaginationStrategy
    from ..pipeline.async_pipeline import AsyncPipeline
    from ..serde import Serde, TypeRef
    from .async_response_handler import AsyncResponseHandler
    from .auth import AuthDescriptor
    from .operation import Operation, OperationInput

__all__ = ["AsyncServiceCore"]

#: A resolved response-type witness accepted by `execute` / `execute_request`.
type _Witness[T] = type[T] | TypeRef[T]


class AsyncServiceCore:
    """Asynchronous executor over the shared pure planners.

    Construct from exactly one of an async transport or a BYO async pipeline:

    - ``AsyncServiceCore(base_url=..., transport=client)`` builds the canonical
      `default_async_pipeline` around ``client`` and *owns* it — closing the
      executor closes ``client``.
    - ``AsyncServiceCore(base_url=..., pipeline=my_pipeline)`` borrows
      ``my_pipeline`` and never closes it.
    """

    __slots__ = (
        "_base_url",
        "_client_auth",
        "_closed",
        "_default_handler",
        "_dispatch_factory",
        "_errors",
        "_owned_transport",
        "_pipeline",
        "_registry",
        "_serde",
    )

    def __init__(
        self,
        *,
        base_url: str | Url,
        pipeline: AsyncPipeline | None = None,
        transport: AsyncHttpClient | None = None,
        errors: StatusErrorMap | None = None,
        serde: Serde | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
        auth: AuthDescriptor | None = None,
        auth_registry: AuthRegistry[AsyncPolicy] | None = None,
    ) -> None:
        """Wire the executor to an async pipeline (BYO) or a transport it builds one around.

        Args:
            base_url: Absolute base URL every operation's path joins onto.
            pipeline: A caller-supplied `AsyncPipeline`. Borrowed, never closed
                by this executor. Mutually exclusive with ``transport``.
            transport: An `AsyncHttpClient` to build the `default_async_pipeline`
                around. Owned — closing the executor closes it. Mutually
                exclusive with ``pipeline``.
            errors: Status → typed-error mapping for the default handler.
                Defaults to an empty map (a bare `HttpResponseError` per error
                status).
            serde: Wire-format serde for request/response bodies. Defaults to
                JSON.
            dispatch_factory: Builds the per-call dispatch context. Defaults to
                `DispatchContext.noop`.
            auth: The client-level authentication requirement — the broadest
                resolver tier, used when neither a per-call nor an operation-level
                requirement is present. ``None`` leaves the client without a
                default requirement.
            auth_registry: The pool of `AuthScheme`s a declared requirement is
                resolved against. ``None`` is an empty registry, so any present
                requirement is unsatisfiable (and raises rather than falling
                through).

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
        self._client_auth = auth
        self._registry: AuthRegistry[AsyncPolicy] = (
            auth_registry if auth_registry is not None else AuthRegistry()
        )
        self._dispatch_factory = (
            dispatch_factory if dispatch_factory is not None else DispatchContext.noop
        )
        self._default_handler: AsyncResponseHandler = AsyncDecodeSuccess(
            self._errors, serde=self._serde
        )
        self._closed = False
        if pipeline is not None:
            self._pipeline = pipeline
            self._owned_transport: AsyncHttpClient | None = None
        else:
            assert transport is not None  # narrowed by the exclusivity guard above
            self._pipeline = default_async_pipeline(transport).build()
            self._owned_transport = transport

    # -- execute --------------------------------------------------------------

    @overload
    async def execute[T](
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: _Witness[T],
        handler: AsyncResponseHandler | None = ...,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> T: ...

    @overload
    async def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: None = ...,
        handler: None = ...,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> AsyncResponse: ...

    @overload
    async def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: None = ...,
        handler: AsyncResponseHandler,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> Any: ...

    async def execute(
        self,
        op: Operation,
        input: OperationInput,
        *,
        response_type: _Witness[Any] | None = None,
        handler: AsyncResponseHandler | None = None,
        auth: AuthDescriptor | AsyncPolicy | None = None,
        **call_options: Any,
    ) -> Any:
        """Run one operation and return its decoded result.

        Args:
            op: The static operation description.
            input: The per-call values (path params, query, headers, body).
            response_type: The type to decode a 2xx body into. When ``None``
                (and no ``handler``), the raw `AsyncResponse` is returned for the
                caller to consume and close.
            handler: An explicit response handler overriding the default
                `AsyncDecodeSuccess`.
            auth: The per-call auth tier. An `AuthDescriptor` is resolved
                against the executor's registry, over the operation's ``auth``
                and the client-level default; a concrete `AsyncPolicy` is a
                verbatim override; ``None`` defers to the operation / client
                tiers.
            **call_options: Per-call overrides forwarded to the pipeline's
                policies via ``ctx.options``.

        Returns:
            The decoded value for a given ``response_type``, the raw
            `AsyncResponse` when neither ``response_type`` nor ``handler`` is
            given, or the handler's result.

        Raises:
            AuthResolutionError: If the selected auth tier is present but no
                registered scheme satisfies it (never falls through to a
                broader tier).
        """
        request = plan_request(op, input, self._base_url, self._serde)
        resolved = self._resolve_auth(op.auth, auth)
        return await self._run(request, resolved, response_type, handler, call_options)

    # -- execute_request ------------------------------------------------------

    @overload
    async def execute_request[T](
        self,
        request: Request,
        *,
        response_type: _Witness[T],
        handler: AsyncResponseHandler | None = ...,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> T: ...

    @overload
    async def execute_request(
        self,
        request: Request,
        *,
        response_type: None = ...,
        handler: None = ...,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> AsyncResponse: ...

    @overload
    async def execute_request(
        self,
        request: Request,
        *,
        response_type: None = ...,
        handler: AsyncResponseHandler,
        auth: AuthDescriptor | AsyncPolicy | None = ...,
        **call_options: Any,
    ) -> Any: ...

    async def execute_request(
        self,
        request: Request,
        *,
        response_type: _Witness[Any] | None = None,
        handler: AsyncResponseHandler | None = None,
        auth: AuthDescriptor | AsyncPolicy | None = None,
        **call_options: Any,
    ) -> Any:
        """Run a pre-built `Request` through the pipeline — the raw-call seam.

        Args:
            request: A fully-formed request to send as-is.
            response_type: The type to decode a 2xx body into, or ``None`` for
                the raw `AsyncResponse`.
            handler: An explicit response handler overriding the default.
            auth: The per-call auth tier. Resolved as for `execute`, but with no
                operation tier — the raw seam knows only the per-call and
                client tiers.
            **call_options: Per-call overrides forwarded to the pipeline.

        Returns:
            The decoded value, the raw `AsyncResponse`, or the handler's result.

        Raises:
            AuthResolutionError: If the selected auth tier is present but
                unsatisfiable.
        """
        resolved = self._resolve_auth(None, auth)
        return await self._run(request, resolved, response_type, handler, call_options)

    def _resolve_auth(
        self, op_auth: AuthDescriptor | None, per_call: AuthDescriptor | AsyncPolicy | None
    ) -> AsyncPolicy | None:
        """Resolve the tiered auth requirements to the async policy to stamp with.

        A concrete `AsyncPolicy` passed per-call is the most specific tier and
        is returned verbatim without descriptor resolution — the BYO override
        path. Otherwise the per-call descriptor (if any), the operation's
        ``auth``, and the client-level default are resolved by tier against the
        registry.

        Args:
            op_auth: The operation-level descriptor, or ``None`` (the raw seam
                passes ``None`` — it has no operation).
            per_call: The per-call tier — a descriptor to resolve, an
                `AsyncPolicy` override to pass through, or ``None``.

        Returns:
            The chosen stamping policy, or ``None`` when no tier is present.

        Raises:
            AuthResolutionError: If the selected tier is present but no
                registered scheme satisfies it.
        """
        if isinstance(per_call, AsyncPolicy):
            return per_call
        resolution = resolve_auth(
            per_call=per_call,
            operation=op_auth,
            client=self._client_auth,
            registry=self._registry,
        )
        return resolution.scheme.policy if resolution is not None else None

    async def _run(
        self,
        request: Request,
        resolved_auth: AsyncPolicy | None,
        response_type: _Witness[Any] | None,
        handler: AsyncResponseHandler | None,
        call_options: Mapping[str, Any],
    ) -> Any:
        """Pack the resolved auth into options, run the pipeline, await the result."""
        options = plan_options(resolved_auth, call_options)
        response = await self._pipeline.run(request, self._dispatch_factory(), **options)
        return await self._handle(response, response_type, handler)

    async def _handle(
        self,
        response: AsyncResponse,
        response_type: _Witness[Any] | None,
        handler: AsyncResponseHandler | None,
    ) -> Any:
        """Apply the planned result handling to ``response``, awaiting the handler."""
        plan = plan_result(handler, self._default_handler, response_type, AsyncResponse)
        if plan is None:
            return response
        chosen, witness = plan
        return await chosen(response, witness)

    # -- paginate / events ----------------------------------------------------

    def paginate[T](
        self,
        op: Operation,
        input: OperationInput,
        strategy: PaginationStrategy[T],
        *,
        max_pages: int | None = None,
        auth: AuthDescriptor | AsyncPolicy | None = None,
        **call_options: Any,
    ) -> AsyncPaginator[T]:
        """Return an `AsyncPaginator` walking the operation's paged sequence.

        The returned paginator is the certified `AsyncPaginator`; its wire-exact
        splice, close-ledger, and cancellation guarantees carry through
        unchanged. Each page fetch runs through this executor's async pipeline
        with the packed per-call options.

        Args:
            op: The static operation description for the first page.
            input: The per-call values for the first page.
            strategy: The `PaginationStrategy` that parses each response into a
                page and derives the next request.
            max_pages: Optional cap on pages fetched. ``None`` is unbounded.
            auth: The per-call auth tier, resolved as for `execute` (over the
                operation and client tiers) before every page fetch.
            **call_options: Per-call overrides forwarded to every page fetch.

        Returns:
            An `AsyncPaginator` over ``strategy``'s element type.

        Raises:
            AuthResolutionError: If the selected auth tier is present but
                unsatisfiable.
        """
        from ..pagination import AsyncPaginator

        resolved = self._resolve_auth(op.auth, auth)
        request = plan_request(op, input, self._base_url, self._serde)
        return AsyncPaginator(
            self._page_sender(resolved, call_options), strategy, request, max_pages=max_pages
        )

    def events[T](
        self,
        op: Operation,
        input: OperationInput,
        mapper: SseMapper[T],
        *,
        auth: AuthDescriptor | AsyncPolicy | None = None,
        **call_options: Any,
    ) -> AsyncTypedSseConnection[T]:
        """Return a typed async SSE connection over the operation's event stream.

        The connection is the certified reconnecting `AsyncSseConnection`
        wrapped in the certified `AsyncTypedSseConnection`, with ``mapper``
        applied. The core SSE layer stays free of any sentinel / serde
        convention — what a data payload means is entirely the mapper's
        decision.

        Args:
            op: The static operation description opening the stream.
            input: The per-call values for the opening request.
            mapper: Maps ``(event_name, data)`` to a typed value or an
                `SseSignal` (skip / done).
            auth: The per-call auth tier, resolved as for `execute` (over the
                operation and client tiers) before every (re)connection.
            **call_options: Per-call overrides forwarded to every (re)connection.

        Returns:
            An `AsyncTypedSseConnection` over ``mapper``'s value type.

        Raises:
            AuthResolutionError: If the selected auth tier is present but
                unsatisfiable.
        """
        from ..http.sse.connection import AsyncSseConnection
        from ..http.sse.typed import AsyncTypedSseConnection

        resolved = self._resolve_auth(op.auth, auth)
        request = plan_request(op, input, self._base_url, self._serde)
        connection = AsyncSseConnection(self._page_sender(resolved, call_options), request)
        return AsyncTypedSseConnection(connection, mapper)

    def _page_sender(
        self, auth: AsyncPolicy | None, call_options: Mapping[str, Any]
    ) -> Callable[[Request], Awaitable[AsyncResponse]]:
        """Build an async send-callable that threads per-call options into each fetch.

        Async paginators and SSE connections accept a bare
        ``Request -> Awaitable[AsyncResponse]`` callable. Passing one (rather
        than the pipeline itself) is how the packed per-call options reach every
        page / reconnection.
        """
        options = plan_options(auth, call_options)

        async def send(request: Request) -> AsyncResponse:
            return await self._pipeline.run(request, self._dispatch_factory(), **options)

        return send

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
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
        aclose = getattr(transport, "aclose", None)
        if callable(aclose):
            result = aclose()
            if isinstance(result, Awaitable):
                await result

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
