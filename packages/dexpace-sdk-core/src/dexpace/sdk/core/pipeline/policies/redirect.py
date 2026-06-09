# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Redirect policy modelled on Java SDK's ``DefaultRedirectStep``.

Walks the response's ``Location`` header through up to ``max_hops``
intermediate responses, following the per-status method/body rules from
RFC 7231 §6.4 and RFC 7538 / RFC 7231 §6.4.7. Credentials are stripped on
every reissue by default (``Authorization`` header dropped, ``userinfo``
in the ``Location`` URL discarded); loops are detected via a visited-URL
set and cause the policy to return the current response instead of
raising.

Status-code matrix:

- ``301`` / ``302``: follow with the original method when it is in
  ``allowed_methods``; otherwise return the response unchanged.
- ``303``: when ``follow_303`` is ``True`` (default), reissue as ``GET``
  with the body dropped and any ``Content-*`` headers removed.
- ``307`` / ``308``: follow with the original method **and body**; the
  body must be replayable or a ``RuntimeError`` is raised.
- Any other status (including other 3xx like ``304``): return unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Literal, cast
from urllib.parse import urljoin

from ...http.common.url import Url
from ...http.request.method import Method
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ...instrumentation import HttpTracer
    from ..context import PipelineContext


_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_CONTENT_HEADER_PREFIX: str = "content-"

#: ``ctx.data`` key holding the per-operation ``HttpTracer``. The first policy
#: in the chain to need it mints one from the call's
#: ``instrumentation_context.http_tracer_factory`` and stores it here so every
#: other policy (tracing, retry, redirect) emits onto the same instance.
HTTP_TRACER_KEY: str = "http_tracer"


def resolve_http_tracer(ctx: PipelineContext) -> HttpTracer:
    """Return the per-operation ``HttpTracer``, minting one on first use.

    The tracer is cached in ``ctx.data[HTTP_TRACER_KEY]`` so every policy in
    the chain shares a single instance for the operation. Defaults to the
    no-op tracer when the call carries the no-op factory, so callers that do
    not instrument pay nothing.

    Args:
        ctx: The pipeline context for the in-flight operation.

    Returns:
        The shared ``HttpTracer`` for this operation.
    """
    existing = ctx.data.get(HTTP_TRACER_KEY)
    if existing is not None:
        return cast("HttpTracer", existing)
    tracer = ctx.call.instrumentation_context.http_tracer_factory.create()
    ctx.data[HTTP_TRACER_KEY] = tracer
    return tracer


class RedirectPolicy(Policy):
    """Follow HTTP redirects per RFC 7231 §6.4 with credential stripping.

    Drives the request through ``self.next`` once per hop. On each 3xx
    response the policy decides whether to reissue based on the status code,
    the request's method, and the ``allowed_methods`` allowlist. Stops at
    the first non-redirect response, when ``max_hops`` is reached, when a
    redirect target has already been visited, or when no ``Location`` header
    is supplied.

    Attributes:
        max_hops: Hard cap on the number of redirect follows. The initial
            request does not count against the cap.
        follow_303: When ``True``, ``303 See Other`` is reissued as ``GET``
            with the body and ``Content-*`` headers dropped. When ``False``,
            ``303`` is returned to the caller unchanged.
        allowed_methods: Methods that are followed on ``301`` / ``302`` /
            ``307`` / ``308``. ``303`` is always rewritten to ``GET`` (which
            is implicitly allowed). Defaults to ``{GET, HEAD}``.
        strip_authorization: When ``True`` (the default), the
            ``Authorization`` header is stripped before every redirect
            reissue. Set ``False`` only when the redirect chain is
            same-origin and the caller has audited the destinations.

    Example:
        ```python
        RedirectPolicy(
            max_hops=5,
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        ```
    """

    STAGE: ClassVar[Literal[Stage.REDIRECT]] = Stage.REDIRECT

    __slots__ = ("allowed_methods", "follow_303", "max_hops", "strip_authorization")

    def __init__(
        self,
        *,
        max_hops: int = 10,
        follow_303: bool = True,
        allowed_methods: frozenset[Method] = frozenset({Method.GET, Method.HEAD}),
        strip_authorization: bool = True,
    ) -> None:
        if max_hops < 0:
            raise ValueError(f"max_hops must be >= 0, got {max_hops}")
        self.max_hops = max_hops
        self.follow_303 = follow_303
        self.allowed_methods = allowed_methods
        self.strip_authorization = strip_authorization

    # ----- main loop ------------------------------------------------------

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        tracer = resolve_http_tracer(ctx)
        tracer.request_url_resolved(str(request.url))
        visited: dict[str, None] = {str(request.url): None}
        hops = 0
        current_request = request
        while True:
            response = self.next.send(current_request, ctx)
            if hops >= self.max_hops:
                return response
            status = int(response.status)
            if status not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            if location is None or not location.strip():
                return response
            next_request = self._build_next_request(current_request, status, location)
            if next_request is None:
                return response
            next_key = str(next_request.url)
            if next_key in visited:
                return response
            visited[next_key] = None
            tracer.request_url_resolved(next_key)
            # Close the intermediate response — we are not handing it back to
            # the caller. The terminal response is closed by the caller via
            # the ``with`` block.
            response.close()
            current_request = next_request
            hops += 1

    # ----- per-hop reissue construction -----------------------------------

    def _build_next_request(
        self,
        request: Request,
        status: int,
        location: str,
    ) -> Request | None:
        """Construct the reissued request for one redirect hop.

        Returns ``None`` when the redirect should not be followed (method
        not in ``allowed_methods`` for 301/302/307/308, or ``follow_303``
        is ``False`` for 303).

        Raises:
            RuntimeError: 307/308 with a non-replayable body.
        """
        next_url = self._resolve_location(request.url, location)
        if status == 303:
            if not self.follow_303:
                return None
            return self._reissue_as_get(request, next_url)
        # 301, 302, 307, 308 all require the original method to be allowed.
        if request.method not in self.allowed_methods:
            return None
        if status in (307, 308):
            return self._reissue_preserving_body(request, next_url)
        # 301 / 302: follow with the original method; body carries over
        # (matches Java's DefaultRedirectStep — caller can downgrade to GET
        # by setting follow_303 plus allowing only safe methods).
        return self._reissue_preserving_body(request, next_url)

    def _resolve_location(self, base: Url, location: str) -> Url:
        """Resolve a possibly-relative Location header into an absolute Url.

        ``userinfo`` from the target URL is dropped — credentials in a
        server-supplied Location header are never honoured.
        """
        absolute = urljoin(str(base), location)
        parsed = Url.parse(absolute)
        if parsed.userinfo is None:
            return parsed
        return replace(parsed, userinfo=None)

    def _reissue_as_get(self, request: Request, next_url: Url) -> Request:
        """Build the reissued GET for a 303 hop.

        Drops the request body and every ``Content-*`` header (per RFC 7231
        §6.4.4 — the body no longer applies to a GET).
        """
        stripped = request.with_method(Method.GET).with_url(next_url).with_body(None)
        for name in tuple(stripped.headers):
            if name.startswith(_CONTENT_HEADER_PREFIX):
                stripped = stripped.without_header(name)
        if self.strip_authorization:
            stripped = stripped.without_header("Authorization")
        return stripped

    def _reissue_preserving_body(self, request: Request, next_url: Url) -> Request:
        """Build the reissued request for 301/302/307/308 hops.

        307/308 must preserve the body, so a non-replayable body raises
        ``RuntimeError`` — sending the same payload twice with a single-use
        body is not possible. 301/302 also carry the body (matches Java's
        ``DefaultRedirectStep``); the same replay requirement applies.
        """
        body = request.body
        if body is not None and not body.is_replayable():
            raise RuntimeError(
                "Cannot follow redirect with a non-replayable request body. "
                "Call body.to_replayable() before sending if redirects are "
                "expected."
            )
        reissued = request.with_url(next_url)
        if self.strip_authorization:
            reissued = reissued.without_header("Authorization")
        return reissued


__all__ = ["HTTP_TRACER_KEY", "RedirectPolicy", "resolve_http_tracer"]
