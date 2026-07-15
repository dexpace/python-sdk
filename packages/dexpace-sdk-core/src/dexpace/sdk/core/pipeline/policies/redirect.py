# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Redirect policy modelled on Java SDK's ``DefaultRedirectStep``.

Walks the response's ``Location`` header through up to ``max_hops``
intermediate responses, following the per-status method/body rules from
RFC 7231 §6.4 and RFC 7538 / RFC 7231 §6.4.7.

Credential hygiene follows the same rules as browsers / OkHttp / the JDK:

- The ``Authorization`` header is stripped before **every** reissue — even a
  same-origin hop and the 303 GET rebuild. Re-stamping a credential for a
  known origin is the auth layer's job (a downstream auth policy re-applies it
  for same-origin reissues), not the redirect layer's.
- On a **cross-origin** hop the origin-scoped ``Cookie`` and
  ``Proxy-Authorization`` headers are additionally stripped. Cross-origin is
  judged against the **seed** (original) request origin — the RFC 6454
  ``(scheme, host, effective_port)`` tuple — not the immediately preceding
  hop, so a same-origin sub-redirect that lands back on a foreign host cannot
  re-expose the caller's credentials.
- ``userinfo`` in the ``Location`` URL is always discarded.
- An HTTPS→HTTP scheme downgrade is rejected by default (the hop is not
  followed); ``allow_scheme_downgrade=True`` permits it but logs a warning.

Loops are detected via a visited-URL set and cause the policy to return the
current response instead of raising.

Status-code matrix:

- ``301`` / ``302``: follow with the original method when it is in
  ``allowed_methods``; otherwise return the response unchanged.
- ``303``: when ``follow_303`` is ``True`` (default), reissue as ``GET`` with
  the body dropped and any ``Content-*`` headers removed.
- ``307`` / ``308``: follow with the original method **and body**; the body
  must be replayable or a ``RuntimeError`` is raised.
- Any other status (including other 3xx like ``304``): return unchanged.

A caller may supply a custom ``should_redirect`` predicate that fully
overrides the built-in follow decision (see `RedirectCondition`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, cast
from urllib.parse import urljoin

from ...http.common.url import Url, _origin
from ...http.request.method import Method
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.common.headers import Headers
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ...http.response.status import Status
    from ...instrumentation import HttpTracer
    from ..context import PipelineContext


_LOGGER = logging.getLogger(__name__)

_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_CONTENT_HEADER_PREFIX: str = "content-"
_AUTHORIZATION_HEADER: str = "authorization"

#: Origin-scoped credential headers stripped only when a reissue crosses
#: origin relative to the seed request (in addition to ``Authorization``,
#: which is stripped on every reissue). Lower-case canonical form.
_CROSS_ORIGIN_CREDENTIAL_HEADERS: tuple[str, ...] = ("cookie", "proxy-authorization")


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


class RedirectResponse(Protocol):
    """Structural read-only view of a response handed to a redirect predicate.

    Both `Response` and `AsyncResponse` satisfy it: a predicate inspects the
    status code and headers to decide whether to follow, without consuming or
    closing the body.

    Attributes:
        status: The response status.
        headers: The response headers.
    """

    @property
    def status(self) -> Status: ...

    @property
    def headers(self) -> Headers: ...


@dataclass(frozen=True, slots=True)
class RedirectCondition:
    """Read-only snapshot handed to a custom ``should_redirect`` predicate.

    A predicate returns ``True`` to follow the redirect (the standard per-hop
    reissue is then built from the ``Location``) or ``False`` to stop and hand
    the current response back. The snapshot is a defensive copy: ``visited_uris``
    is an immutable tuple, so a predicate cannot mutate the live cycle-detection
    state.

    Attributes:
        response: The current response being evaluated.
        redirect_count: The number of redirects already followed on this
            operation — ``0`` on the first decision.
        visited_uris: Insertion-ordered URIs visited so far, including the
            current request's URI. Immutable.
    """

    response: RedirectResponse
    redirect_count: int
    visited_uris: tuple[str, ...]


#: A custom redirect predicate. Given a read-only `RedirectCondition`, returns
#: ``True`` to follow the redirect or ``False`` to stop. When configured it
#: fully overrides the built-in follow decision (status matrix, method
#: allow-list, ``follow_303``).
type RedirectPredicate = Callable[[RedirectCondition], bool]


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
        strip_authorization: When ``True`` (the default), the ``Authorization``
            header is stripped before **every** reissue and the origin-scoped
            ``Cookie`` / ``Proxy-Authorization`` headers are additionally
            stripped on a cross-origin reissue. Set ``False`` to never strip
            any credential, e.g. when the caller has audited every destination
            in the chain.
        allow_scheme_downgrade: When ``False`` (the default), an HTTPS→HTTP
            scheme downgrade on a hop is not followed (the current response is
            handed back). When ``True``, the downgrade is followed and a
            warning is logged; credential stripping still applies.
        should_redirect: Optional custom predicate that fully overrides the
            built-in follow decision (see `RedirectCondition`).

    Example:
        ```python
        RedirectPolicy(
            max_hops=5,
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        ```
    """

    STAGE: ClassVar[Literal[Stage.REDIRECT]] = Stage.REDIRECT

    __slots__ = (
        "allow_scheme_downgrade",
        "allowed_methods",
        "follow_303",
        "max_hops",
        "should_redirect",
        "strip_authorization",
    )

    def __init__(
        self,
        *,
        max_hops: int = 10,
        follow_303: bool = True,
        allowed_methods: frozenset[Method] = frozenset({Method.GET, Method.HEAD}),
        strip_authorization: bool = True,
        allow_scheme_downgrade: bool = False,
        should_redirect: RedirectPredicate | None = None,
    ) -> None:
        if max_hops < 0:
            raise ValueError(f"max_hops must be >= 0, got {max_hops}")
        self.max_hops = max_hops
        self.follow_303 = follow_303
        self.allowed_methods = allowed_methods
        self.strip_authorization = strip_authorization
        self.allow_scheme_downgrade = allow_scheme_downgrade
        self.should_redirect = should_redirect

    # ----- main loop ------------------------------------------------------

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        tracer = resolve_http_tracer(ctx)
        tracer.request_url_resolved(str(request.url))
        seed_origin = _origin(request.url)
        visited: dict[str, None] = {str(request.url): None}
        hops = 0
        current_request = request
        while True:
            response = self.next.send(current_request, ctx)
            if hops >= self.max_hops:
                return response
            try:
                next_request = self._decide_next(
                    current_request, response, seed_origin, hops, tuple(visited)
                )
            except RuntimeError:
                # A body-preserving redirect with a non-replayable body cannot
                # be reissued. Close the in-hand 3xx response before the error
                # escapes so the connection is not leaked.
                response.close()
                raise
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

    # ----- per-hop decision -----------------------------------------------

    def _decide_next(
        self,
        request: Request,
        response: RedirectResponse,
        seed_origin: tuple[str, str, int | None],
        hops: int,
        visited: tuple[str, ...],
    ) -> Request | None:
        """Compute the reissued request for one hop, or ``None`` to stop.

        ``None`` means "hand the current response back unfollowed". A custom
        ``should_redirect`` predicate, when set, fully overrides the built-in
        follow decision; otherwise the status-code matrix decides.

        Raises:
            RuntimeError: 307/308 (or a predicate-authorised body-preserving
                reissue) with a non-replayable body.
        """
        status = int(response.status)
        predicate = self.should_redirect
        if predicate is not None:
            condition = RedirectCondition(
                response=response, redirect_count=hops, visited_uris=visited
            )
            if not predicate(condition):
                return None
        elif status not in _REDIRECT_STATUSES:
            return None
        location = response.headers.get("Location")
        if location is None or not location.strip():
            return None
        return self._build_next_request(
            request, status, location, seed_origin, predicate_authorized=predicate is not None
        )

    # ----- per-hop reissue construction -----------------------------------

    def _build_next_request(
        self,
        request: Request,
        status: int,
        location: str,
        seed_origin: tuple[str, str, int | None],
        *,
        predicate_authorized: bool = False,
    ) -> Request | None:
        """Construct the reissued request for one redirect hop.

        Returns ``None`` when the redirect should not be followed — a malformed
        ``Location`` that cannot be resolved, a denied HTTPS→HTTP downgrade, a
        method not in ``allowed_methods`` for 301/302/307/308, or
        ``follow_303`` being ``False`` for 303. ``predicate_authorized``
        bypasses the method-allowlist and ``follow_303`` gates (the caller's
        predicate already decided to follow). The caller treats ``None`` as
        "stop and hand back the current response unfollowed", leaving that
        response open.

        Cross-origin is judged against ``seed_origin`` (the original request's
        origin), not this hop's origin, so a same-origin sub-redirect on a
        foreign host still strips origin-scoped credentials.

        Raises:
            RuntimeError: 307/308 (or a predicate-authorised reissue) with a
                non-replayable body.
        """
        try:
            next_url = self._resolve_location(request.url, location)
        except ValueError:
            # A malformed Location header (bad scheme/host, un-parseable
            # authority, …) has no resolvable target. Signal "do not follow" so
            # the loop hands the response back unfollowed and open — the same
            # disposition as loop detection / max-hops exhaustion.
            return None
        if self._is_scheme_downgrade(request.url, next_url):
            if not self.allow_scheme_downgrade:
                return None
            _LOGGER.warning(
                "Following HTTPS->HTTP redirect downgrade from host %s to %s "
                "(allow_scheme_downgrade=True)",
                request.url.host,
                next_url.host,
            )
        cross_origin = seed_origin != _origin(next_url)
        if status == 303:
            if not predicate_authorized and not self.follow_303:
                return None
            return self._reissue_as_get(request, next_url, cross_origin=cross_origin)
        # 301, 302, 307, 308 (and any predicate-authorised status) require the
        # original method to be allowed unless a predicate overrode the gate.
        if not predicate_authorized and request.method not in self.allowed_methods:
            return None
        return self._reissue_preserving_body(request, next_url, cross_origin=cross_origin)

    @staticmethod
    def _is_scheme_downgrade(from_url: Url, to_url: Url) -> bool:
        """Report whether ``from_url``→``to_url`` drops HTTPS down to HTTP."""
        return from_url.scheme.lower() == "https" and to_url.scheme.lower() == "http"

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

    def _strip_credentials(self, request: Request, *, cross_origin: bool) -> Request:
        """Strip credential headers from a reissued request.

        ``Authorization`` is stripped on every reissue; the origin-scoped
        ``Cookie`` / ``Proxy-Authorization`` headers are additionally stripped
        when the reissue crosses origin. All stripping is gated by
        ``strip_authorization``.

        Args:
            request: The request being reissued.
            cross_origin: Whether the reissue crosses the seed origin.
        """
        if not self.strip_authorization:
            return request
        stripped = request.without_header(_AUTHORIZATION_HEADER)
        if cross_origin:
            for name in _CROSS_ORIGIN_CREDENTIAL_HEADERS:
                stripped = stripped.without_header(name)
        return stripped

    def _reissue_as_get(
        self,
        request: Request,
        next_url: Url,
        *,
        cross_origin: bool,
    ) -> Request:
        """Build the reissued GET for a 303 hop.

        Drops the request body and every ``Content-*`` header (per RFC 7231
        §6.4.4 — the body no longer applies to a GET), then strips credentials
        per `_strip_credentials`.

        Args:
            request: The request being redirected (the current hop).
            next_url: The resolved absolute target of the redirect.
            cross_origin: Whether ``next_url`` differs in origin from the seed.
        """
        stripped = request.with_method(Method.GET).with_url(next_url).with_body(None)
        for name in tuple(stripped.headers):
            if name.startswith(_CONTENT_HEADER_PREFIX):
                stripped = stripped.without_header(name)
        return self._strip_credentials(stripped, cross_origin=cross_origin)

    def _reissue_preserving_body(
        self,
        request: Request,
        next_url: Url,
        *,
        cross_origin: bool,
    ) -> Request:
        """Build the reissued request for 301/302/307/308 hops.

        307/308 must preserve the body, so a non-replayable body raises
        ``RuntimeError`` — sending the same payload twice with a single-use
        body is not possible. 301/302 also carry the body (matches Java's
        ``DefaultRedirectStep``); the same replay requirement applies.
        Credentials are stripped per `_strip_credentials`.

        Args:
            request: The request being redirected (the current hop).
            next_url: The resolved absolute target of the redirect.
            cross_origin: Whether ``next_url`` differs in origin from the seed.

        Raises:
            RuntimeError: A non-replayable body cannot be reissued.
        """
        body = request.body
        if body is not None and not body.is_replayable():
            raise RuntimeError(
                "Cannot follow redirect with a non-replayable request body. "
                "Call body.to_replayable() before sending if redirects are "
                "expected."
            )
        reissued = request.with_url(next_url)
        return self._strip_credentials(reissued, cross_origin=cross_origin)


__all__ = [
    "HTTP_TRACER_KEY",
    "RedirectCondition",
    "RedirectPolicy",
    "RedirectPredicate",
    "RedirectResponse",
    "resolve_http_tracer",
]
