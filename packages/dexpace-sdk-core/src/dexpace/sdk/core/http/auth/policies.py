# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Built-in authentication pipeline policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...errors import ClientAuthenticationError, ServiceRequestError
from ...pipeline.async_policy import AsyncPolicy
from ...pipeline.policy import Policy
from ...pipeline.stage import Stage
from ...util.clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock
from ..common.url import _origin
from .access_token import AccessTokenInfo, TokenRequestOptions
from .challenge import parse_challenges
from .challenge_handler import ChallengeHandler
from .credentials import (
    AsyncTokenCredential,
    BasicAuthCredential,
    KeyCredential,
    TokenCredential,
)
from .token_cache import (
    InMemoryTokenCache,
    TokenCache,
    _AsyncSingleFlight,
    _SyncSingleFlight,
)

if TYPE_CHECKING:
    from ...pipeline.context import PipelineContext
    from ..common.url import Url
    from ..request.request import Request
    from ..response.async_response import AsyncResponse
    from ..response.response import Response


class KeyCredentialPolicy(Policy):
    """Stamp a configured header from a ``KeyCredential``.

    SansIO-shaped (no chain wrapping needed) but implemented as a ``Policy``
    so it integrates uniformly with the rest of the pipeline.

    The credential header is stamped only while the request stays on the
    origin recorded on the first pass through the policy. When a downstream
    redirect reissues the request against a different origin (scheme, host,
    or effective port), the credential is withheld so it never reaches a
    foreign host. On the same-origin stamping path the key is refused for a
    non-HTTPS URL (opt out per call with ``enforce_https=False``) so it is
    never read or written onto a plaintext request.

    Attributes:
        header_name: Header to write.
        prefix: Optional prefix (with trailing space) for the header value.
    """

    STAGE = Stage.AUTH
    __slots__ = ("_credential", "header_name", "prefix")

    def __init__(
        self,
        credential: KeyCredential,
        header_name: str,
        *,
        prefix: str | None = None,
    ) -> None:
        if not isinstance(credential, KeyCredential):
            raise TypeError("credential must be a KeyCredential")
        if not header_name:
            raise ValueError("header_name must not be empty")
        self._credential = credential
        self.header_name = header_name
        self.prefix = f"{prefix} " if prefix else ""

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if _crosses_recorded_origin(request, ctx):
            return self.next.send(request, ctx)
        _enforce_https(request, ctx)
        value = f"{self.prefix}{self._credential.key}"
        return self.next.send(request.with_header(self.header_name, value), ctx)

    def __repr__(self) -> str:
        return (
            f"KeyCredentialPolicy(credential={self._credential!r}, "
            f"header_name={self.header_name!r}, prefix={self.prefix!r})"
        )


class BasicAuthPolicy(Policy):
    """Stamp ``Authorization: Basic <base64>`` from a ``BasicAuthCredential``.

    The credential is stamped only while the request stays on the origin
    recorded on the first pass through the policy. When a downstream redirect
    reissues the request against a different origin (scheme, host, or
    effective port), the credential is withheld so it never reaches a foreign
    host. On the same-origin stamping path the payload is refused for a
    non-HTTPS URL (opt out per call with ``enforce_https=False``) so it is
    never read or written onto a plaintext request.
    """

    STAGE = Stage.AUTH
    __slots__ = ("_credential",)

    def __init__(self, credential: BasicAuthCredential) -> None:
        if not isinstance(credential, BasicAuthCredential):
            raise TypeError("credential must be a BasicAuthCredential")
        self._credential = credential

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        if _crosses_recorded_origin(request, ctx):
            return self.next.send(request, ctx)
        _enforce_https(request, ctx)
        value = f"Basic {self._credential.encoded}"
        return self.next.send(request.with_header("Authorization", value), ctx)

    def __repr__(self) -> str:
        return f"BasicAuthPolicy(credential={self._credential!r})"


class BearerTokenPolicy(Policy):
    """Acquire and apply an OAuth bearer token.

    Caches a single token in memory by default; pass a shared ``TokenCache``
    to share tokens across credentials. Refreshes proactively as the token
    nears expiry, or after a 401 response with ``WWW-Authenticate``. Enforces
    HTTPS unless ``enforce_https=False`` is passed in ``ctx.options``.

    The token is acquired and stamped only while the request stays on the
    origin recorded on the first pass. When a downstream redirect reissues the
    request against a different origin (scheme, host, or effective port), the
    policy forwards the request unchanged — it does not acquire, refresh, or
    stamp a token — so the bearer token never reaches a foreign host. The
    HTTPS-enforcement check applies only on that same-origin stamping path.

    Token serving is delegated to a `_SyncSingleFlight` coordinator: reads of
    the published token are wait-free, and concurrent refreshes are collapsed
    under a ``threading.Lock`` (double-checked) so the credential's
    ``get_token_info`` runs at most once per refresh window even under heavy
    concurrent send pressure. A token is refreshed once it comes within 30
    seconds of expiry; a provider that returns ``None`` or an already-expired
    token raises rather than caching a bad token, and a throwing provider
    propagates without being cached so the next request retries it.

    The ``on_challenge`` hook is a no-op by default; subclasses override it
    to handle CAE/claims challenges. Alternatively, callers can pass a
    ``challenge_handler`` to plug in a scheme-aware handler (e.g.
    ``DigestChallengeHandler``); the handler is consulted before
    ``on_challenge`` on 401/407 and, if it satisfies the challenge, the
    returned ``(name, value)`` pair is stamped on the retried request.
    """

    STAGE = Stage.AUTH
    __slots__ = (
        "_audience",
        "_cache",
        "_challenge_handler",
        "_credential",
        "_scopes",
        "_single_flight",
    )

    def __init__(
        self,
        credential: TokenCredential,
        *scopes: str,
        cache: TokenCache | None = None,
        audience: str | None = None,
        clock: Clock | None = None,
        challenge_handler: ChallengeHandler | None = None,
    ) -> None:
        if not scopes:
            raise ValueError("at least one scope is required")
        self._credential = credential
        self._scopes = scopes
        self._cache: TokenCache = cache or InMemoryTokenCache()
        self._audience = audience
        resolved_clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._single_flight = _SyncSingleFlight(self._cache, scopes, audience, resolved_clock)
        self._challenge_handler = challenge_handler

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        request = self._authorize(request, ctx)
        response = self.next.send(request, ctx)
        status = int(response.status)
        if status not in (401, 407):
            return response
        # On 401 the origin rejected the bearer token: drop it from the cache
        # so a later refresh re-fetches. Evict only when the cached token still
        # matches the one this request carried, so a stale 401 cannot discard a
        # token a concurrent refresh already replaced. A 407 means the proxy
        # rejected us, not the origin — leave the cached token alone.
        if status == 401:
            self._evict_rejected_token(request)
        handler_header = self._apply_challenge_handler(request, response, status)
        if handler_header is not None:
            request = request.with_header(*handler_header)
            # The rejected challenge response is not handed back; close it to
            # release the pooled connection before the authenticated retry.
            response.close()
            response = self.next.send(request, ctx)
            if int(response.status) not in (401, 407):
                return response
            raise ClientAuthenticationError(response=response)
        if status != 401:
            # No handler match on a 407; surface the response unchanged so
            # callers can inspect proxy-auth failures themselves.
            return response
        return self._replay_on_challenge(request, response, ctx)

    def _replay_on_challenge(
        self,
        request: Request,
        response: Response,
        ctx: PipelineContext,
    ) -> Response:
        """Consult ``on_challenge`` and replay the 401 at most once.

        A 401 without a ``WWW-Authenticate`` header is surfaced unchanged and
        the hook is never consulted. A single-use request body is likewise
        surfaced un-replayed rather than corrupting a half-consumed stream by
        resending it. When the hook authorizes a retry and the body is
        replayable, the request is re-issued once with a refreshed token and
        the resulting response is returned regardless of its status — there is
        no challenge loop. If the hook raises, the rejected response is closed
        before the exception propagates so its connection is not leaked.

        Args:
            request: The request that produced the 401.
            response: The open 401 response.
            ctx: The per-operation pipeline context.

        Returns:
            The surfaced 401, or the response from the single replay.
        """
        if "WWW-Authenticate" not in response.headers:
            return response
        if request.body is not None and not request.body.is_replayable():
            return response
        try:
            proceed = self.on_challenge(request, response)
        except BaseException:
            response.close()
            raise
        if not proceed:
            return response
        request = self._authorize(request, ctx, force_refresh=True)
        # Release the rejected 401 before retrying with the refreshed token.
        response.close()
        return self.next.send(request, ctx)

    def _evict_rejected_token(self, request: Request) -> None:
        """Evict the cached token when it still matches this request's stamp.

        A 401 invalidates only the exact token that was sent. Matching the
        stamped ``Authorization`` header against the cached token guards a
        token a concurrent refresh already installed: a stale 401 for the
        superseded token must not discard the newer one still valid for other
        requests. The compare-and-set runs under the refresh lock so it is
        atomic with respect to ``_authorize``.

        Args:
            request: The request whose stamped token was rejected.
        """
        stamped = request.headers.get("Authorization")
        if stamped is None:
            return
        self._single_flight.evict_if_matches(
            lambda cached: stamped == f"{cached.token_type} {cached.token}",
            _expired_token(),
        )

    def _apply_challenge_handler(
        self,
        request: Request,
        response: Response,
        status: int,
    ) -> tuple[str, str] | None:
        """Delegate to the configured ``ChallengeHandler``, if any.

        Returns the ``(name, value)`` pair to stamp on the retry, or ``None``
        when no handler is configured or the handler declines the challenge.
        """
        if self._challenge_handler is None:
            return None
        is_proxy = status == 407
        header_name = "Proxy-Authenticate" if is_proxy else "WWW-Authenticate"
        raw = response.headers.get(header_name)
        if raw is None:
            return None
        challenges = parse_challenges(raw)
        if not challenges or not self._challenge_handler.can_handle(challenges):
            return None
        return self._challenge_handler.handle(
            request.method,
            request.url,
            challenges,
            is_proxy=is_proxy,
        )

    def on_challenge(self, request: Request, response: Response) -> bool:
        """Override to handle a ``WWW-Authenticate`` challenge.

        Args:
            request: The request that produced the 401.
            response: The 401 response.

        Returns:
            ``True`` to re-issue the request after acquiring a new token;
            ``False`` (the default) to surface the error to the caller.

        Note:
            By the time this hook runs, ``send`` has invalidated the cached
            token for ``(scopes, audience)`` — replacing it with an expired
            sentinel (``token=""``, ``expires_on=0``) — but only when the
            cached token still matched the one stamped on ``request``. A
            subclass inspecting the ``TokenCache`` here therefore sees the
            sentinel whenever the rejection was for the currently cached
            token, and the newer token otherwise. When this hook returns
            ``True`` the subsequent ``_authorize`` call forces a fresh
            acquisition regardless.
        """
        del request, response
        return False

    def __repr__(self) -> str:
        return _bearer_repr(self)

    def _authorize(
        self,
        request: Request,
        ctx: PipelineContext,
        *,
        force_refresh: bool = False,
    ) -> Request:
        # A redirect that crossed origin must not receive the bearer token:
        # forward the request unchanged without acquiring or refreshing one,
        # and skip the HTTPS enforcement that only governs the stamping path.
        if _crosses_recorded_origin(request, ctx):
            return request
        _enforce_https(request, ctx)
        options = _token_options(ctx.options)

        def _provider() -> AccessTokenInfo:
            return self._credential.get_token_info(*self._scopes, options=options)

        token = self._single_flight.get_token(_provider, force=force_refresh)
        return request.with_header("Authorization", f"{token.token_type} {token.token}")


class AsyncBearerTokenPolicy(AsyncPolicy):
    """Async twin of ``BearerTokenPolicy``.

    Token serving is delegated to an `_AsyncSingleFlight` coordinator that
    implements a three-zone refresh strategy: a fresh token is served
    immediately; a token inside the 30-second refresh margin is served while a
    non-fatal background refresh is kicked off; and an expired (or absent)
    token blocks the caller on a single shared fetch. That fetch is guarded by
    an ``asyncio.Lock`` and awaited through ``asyncio.shield`` so the
    ``AsyncTokenCredential`` runs at most once per refresh window and one
    waiter's cancellation cannot abort the fetch the others depend on. A
    provider that returns ``None`` or an already-expired token raises, and a
    throwing provider propagates without being cached.

    Like the sync policy, an optional ``challenge_handler`` plugs in a
    scheme-aware handler (e.g. ``DigestChallengeHandler``); it is consulted
    before ``on_challenge`` on 401/407 and, if it satisfies the challenge,
    the returned ``(name, value)`` pair is stamped on the retried request.
    A 401 invalidates the cached origin token; a 407 leaves it alone because
    the proxy, not the origin, rejected the request.

    The token is acquired and stamped only while the request stays on the
    origin recorded on the first pass. When a downstream redirect reissues the
    request against a different origin (scheme, host, or effective port), the
    policy forwards the request unchanged — it does not acquire, refresh, or
    stamp a token — so the bearer token never reaches a foreign host. The
    HTTPS-enforcement check applies only on that same-origin stamping path.
    """

    STAGE = Stage.AUTH
    __slots__ = (
        "_audience",
        "_cache",
        "_challenge_handler",
        "_credential",
        "_scopes",
        "_single_flight",
    )

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *scopes: str,
        cache: TokenCache | None = None,
        audience: str | None = None,
        clock: AsyncClock | None = None,
        challenge_handler: ChallengeHandler | None = None,
    ) -> None:
        if not scopes:
            raise ValueError("at least one scope is required")
        self._credential = credential
        self._scopes = scopes
        self._cache: TokenCache = cache or InMemoryTokenCache()
        self._audience = audience
        # Only ``clock.now()`` is consulted by the coordinator (a sync read on
        # both Clock variants), so an ``AsyncClock`` slots in without its
        # awaitable ``sleep`` ever being needed.
        resolved_clock: AsyncClock = clock if clock is not None else ASYNC_SYSTEM_CLOCK
        self._single_flight = _AsyncSingleFlight(self._cache, scopes, audience, resolved_clock)
        self._challenge_handler = challenge_handler

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        request = await self._authorize(request, ctx)
        response = await self.next.send(request, ctx)
        status = int(response.status)
        if status not in (401, 407):
            return response
        # On 401 the origin rejected the bearer token: drop it from the cache
        # so a later refresh re-fetches. Evict only when the cached token still
        # matches the one this request carried, so a stale 401 cannot discard a
        # token a concurrent refresh already replaced. A 407 means the proxy
        # rejected us, not the origin — leave the cached token alone.
        if status == 401:
            self._evict_rejected_token(request)
        handler_header = self._apply_challenge_handler(request, response, status)
        if handler_header is not None:
            request = request.with_header(*handler_header)
            # The rejected challenge response is not handed back; close it to
            # release the pooled connection before the authenticated retry.
            await response.close()
            response = await self.next.send(request, ctx)
            if int(response.status) not in (401, 407):
                return response
            raise ClientAuthenticationError(response=response)
        if status != 401:
            # No handler match on a 407; surface the response unchanged so
            # callers can inspect proxy-auth failures themselves.
            return response
        return await self._replay_on_challenge(request, response, ctx)

    async def _replay_on_challenge(
        self,
        request: Request,
        response: AsyncResponse,
        ctx: PipelineContext,
    ) -> AsyncResponse:
        """Async twin of ``BearerTokenPolicy._replay_on_challenge``.

        Enforces the same gate as the sync policy: a 401 without a
        ``WWW-Authenticate`` header is surfaced without consulting the hook, a
        single-use body is surfaced un-replayed, and at most one replay is made
        when the hook authorizes it, whose response is returned regardless of
        status. Wrapping the ``await`` of ``on_challenge`` catches every hook
        failure shape — a raised coroutine and a synchronous throw from
        producing the coroutine alike — so the rejected response is closed
        before the exception propagates.

        Args:
            request: The request that produced the 401.
            response: The open 401 response.
            ctx: The per-operation pipeline context.

        Returns:
            The surfaced 401, or the response from the single replay.
        """
        if "WWW-Authenticate" not in response.headers:
            return response
        if request.body is not None and not request.body.is_replayable():
            return response
        try:
            proceed = await self.on_challenge(request, response)
        except BaseException:
            await response.close()
            raise
        if not proceed:
            return response
        request = await self._authorize(request, ctx, force_refresh=True)
        # Release the rejected 401 before retrying with the refreshed token.
        await response.close()
        return await self.next.send(request, ctx)

    def _evict_rejected_token(self, request: Request) -> None:
        """Evict the cached token when it still matches this request's stamp.

        Mirrors ``BearerTokenPolicy._evict_rejected_token``. The compare-and-set
        needs no lock here: it runs without an ``await`` between the read and
        the write, so no other task can interleave under asyncio's cooperative
        scheduling.

        Args:
            request: The request whose stamped token was rejected.
        """
        stamped = request.headers.get("Authorization")
        if stamped is None:
            return
        cached = self._cache.get(self._scopes, self._audience)
        if cached is not None and stamped == f"{cached.token_type} {cached.token}":
            self._cache.set(self._scopes, _expired_token(), self._audience)

    def _apply_challenge_handler(
        self,
        request: Request,
        response: AsyncResponse,
        status: int,
    ) -> tuple[str, str] | None:
        """Delegate to the configured ``ChallengeHandler``, if any.

        Returns the ``(name, value)`` pair to stamp on the retry, or ``None``
        when no handler is configured or the handler declines the challenge.
        """
        if self._challenge_handler is None:
            return None
        is_proxy = status == 407
        header_name = "Proxy-Authenticate" if is_proxy else "WWW-Authenticate"
        raw = response.headers.get(header_name)
        if raw is None:
            return None
        challenges = parse_challenges(raw)
        if not challenges or not self._challenge_handler.can_handle(challenges):
            return None
        return self._challenge_handler.handle(
            request.method,
            request.url,
            challenges,
            is_proxy=is_proxy,
        )

    async def on_challenge(
        self,
        request: Request,
        response: AsyncResponse,
    ) -> bool:
        """Async version of ``BearerTokenPolicy.on_challenge``.

        Note:
            By the time this hook runs, ``send`` has invalidated the cached
            token for ``(scopes, audience)`` — replacing it with an expired
            sentinel (``token=""``, ``expires_on=0``) — but only when the
            cached token still matched the one stamped on ``request``. A
            subclass inspecting the ``TokenCache`` here therefore sees the
            sentinel whenever the rejection was for the currently cached
            token, and the newer token otherwise. When this hook returns
            ``True`` the subsequent ``_authorize`` call forces a fresh
            acquisition regardless.
        """
        del request, response
        return False

    def __repr__(self) -> str:
        return _bearer_repr(self)

    async def _authorize(
        self,
        request: Request,
        ctx: PipelineContext,
        *,
        force_refresh: bool = False,
    ) -> Request:
        # A redirect that crossed origin must not receive the bearer token:
        # forward the request unchanged without acquiring or refreshing one,
        # and skip the HTTPS enforcement that only governs the stamping path.
        if _crosses_recorded_origin(request, ctx):
            return request
        _enforce_https(request, ctx)
        options = _token_options(ctx.options)

        async def _provider() -> AccessTokenInfo:
            return await self._credential.get_token_info(*self._scopes, options=options)

        token = await self._single_flight.get_token(_provider, force=force_refresh)
        return request.with_header("Authorization", f"{token.token_type} {token.token}")


_AUTH_ORIGIN_KEY: str = "_auth_origin"


def _crosses_recorded_origin(request: Request, ctx: PipelineContext) -> bool:
    """Report whether ``request`` left the origin recorded for this operation.

    On the first pass through an auth policy the request's origin is stored in
    ``ctx.data`` (which is per-operation), so a later redirect reissue can be
    compared against it. When the current origin differs, the credential must
    not be stamped — this is what stops a redirect to a foreign host from
    receiving the caller's credentials.

    Args:
        request: The request the auth policy is about to forward.
        ctx: The per-operation pipeline context.

    Returns:
        ``True`` when the request's origin differs from the one recorded on
        the first pass; ``False`` on the first pass or a same-origin reissue.
    """
    current = _origin(request.url)
    recorded: tuple[str, str, int | None] = ctx.data.setdefault(_AUTH_ORIGIN_KEY, current)
    return recorded != current


def _is_https(url: Url) -> bool:
    """Return True if ``url``'s scheme is ``https`` (case-insensitive)."""
    return url.scheme.lower() == "https"


def _enforce_https(request: Request, ctx: PipelineContext) -> None:
    """Refuse to credential a non-HTTPS request unless enforcement is disabled.

    Every credentialed policy calls this on its same-origin stamping path,
    before the secret is fetched or read, so a credential is never prepared
    for a plaintext-HTTP request. Enforcement is on by default; pass
    ``enforce_https=False`` in the per-call options to opt out.

    Args:
        request: The request an auth policy is about to credential.
        ctx: The per-operation pipeline context.

    Raises:
        ServiceRequestError: When the URL is not HTTPS and enforcement is on.
    """
    if ctx.options.get("enforce_https", True) and not _is_https(request.url):
        raise ServiceRequestError("Credential authentication is not permitted for non-HTTPS URLs.")


def _bearer_repr(policy: BearerTokenPolicy | AsyncBearerTokenPolicy) -> str:
    """Render a bearer policy without exposing the lazily fetched token.

    The token is never held at construction; only the non-secret scopes,
    audience, and credential type name are shown.
    """
    return (
        f"{type(policy).__name__}(credential={type(policy._credential).__name__}, "
        f"scopes={policy._scopes!r}, audience={policy._audience!r})"
    )


def _token_options(call_options: dict[str, Any]) -> TokenRequestOptions | None:
    """Project ``ctx.options`` onto ``TokenRequestOptions``."""
    options: TokenRequestOptions = {}
    for key in TokenRequestOptions.__annotations__:
        if key in call_options:
            options[key] = call_options[key]  # type: ignore[literal-required]
    return options or None


def _expired_token() -> AccessTokenInfo:
    """Sentinel token used to invalidate the cache after a 401."""
    return AccessTokenInfo(token="", expires_on=0)


__all__ = [
    "AsyncBearerTokenPolicy",
    "BasicAuthPolicy",
    "BearerTokenPolicy",
    "KeyCredentialPolicy",
]
