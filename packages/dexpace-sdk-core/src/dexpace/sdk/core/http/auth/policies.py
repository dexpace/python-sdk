# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Built-in authentication pipeline policies."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from ...errors import ClientAuthenticationError, ServiceRequestError
from ...pipeline.async_policy import AsyncPolicy
from ...pipeline.policy import Policy
from ...pipeline.stage import Stage
from ...util.clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock
from .access_token import AccessTokenInfo, TokenRequestOptions
from .challenge import parse_challenges
from .challenge_handler import ChallengeHandler
from .credentials import (
    AsyncTokenCredential,
    BasicAuthCredential,
    KeyCredential,
    TokenCredential,
)
from .token_cache import InMemoryTokenCache, TokenCache

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
        value = f"{self.prefix}{self._credential.key}"
        return self.next.send(request.with_header(self.header_name, value), ctx)


class BasicAuthPolicy(Policy):
    """Stamp ``Authorization: Basic <base64>`` from a ``BasicAuthCredential``."""

    STAGE = Stage.AUTH
    __slots__ = ("_credential",)

    def __init__(self, credential: BasicAuthCredential) -> None:
        if not isinstance(credential, BasicAuthCredential):
            raise TypeError("credential must be a BasicAuthCredential")
        self._credential = credential

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        value = f"Basic {self._credential.encoded}"
        return self.next.send(request.with_header("Authorization", value), ctx)


class BearerTokenPolicy(Policy):
    """Acquire and apply an OAuth bearer token.

    Caches a single token in memory by default; pass a shared ``TokenCache``
    to share tokens across credentials. Refreshes when ``needs_refresh``
    returns True, or after a 401 response with ``WWW-Authenticate``. Enforces
    HTTPS unless ``enforce_https=False`` is passed in ``ctx.options``.

    Concurrent refreshes are serialized via a ``threading.Lock`` using a
    double-checked pattern so the credential's ``get_token_info`` is invoked
    at most once per refresh window even under heavy concurrent send pressure.

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
        "_clock",
        "_credential",
        "_lock",
        "_scopes",
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
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._lock = threading.Lock()
        self._challenge_handler = challenge_handler

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        request = self._authorize(request, ctx)
        response = self.next.send(request, ctx)
        status = int(response.status)
        if status not in (401, 407):
            return response
        # On 401 the bearer token was rejected: drop it from the cache so the
        # ``on_challenge`` fallback path forces a refresh. A 407 means the
        # proxy rejected us, not the origin — leave the cached token alone.
        if status == 401:
            self._cache.set(self._scopes, _expired_token(), self._audience)
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
        if "WWW-Authenticate" in response.headers and self.on_challenge(request, response):
            request = self._authorize(request, ctx, force_refresh=True)
            # Release the rejected 401 before retrying with the refreshed token.
            response.close()
            response = self.next.send(request, ctx)
            if int(response.status) != 401:
                return response
        raise ClientAuthenticationError(response=response)

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
            By the time this hook runs, ``send`` has already replaced the
            cached token for ``(scopes, audience)`` with an expired sentinel
            (``token=""``, ``expires_on=0``). Subclasses that inspect the
            ``TokenCache`` here will see that sentinel, not the original
            token that was attached to ``request``. The sentinel is what
            forces the subsequent ``_authorize`` call (when this hook
            returns ``True``) to invoke the credential and acquire a fresh
            token rather than reusing the rejected one.
        """
        del request, response
        return False

    def _authorize(
        self,
        request: Request,
        ctx: PipelineContext,
        *,
        force_refresh: bool = False,
    ) -> Request:
        if ctx.options.get("enforce_https", True) and not _is_https(request.url):
            raise ServiceRequestError(
                "Bearer token authentication is not permitted for non-HTTPS URLs."
            )
        token = self._cache.get(self._scopes, self._audience)
        if force_refresh or token is None or token.needs_refresh(clock=self._clock):
            with self._lock:
                # Double-checked: another thread may have refreshed while we waited.
                token = self._cache.get(self._scopes, self._audience)
                if force_refresh or token is None or token.needs_refresh(clock=self._clock):
                    options = _token_options(ctx.options)
                    token = self._credential.get_token_info(*self._scopes, options=options)
                    self._cache.set(self._scopes, token, self._audience)
        assert token is not None  # Narrowed by the refresh branch above.
        return request.with_header("Authorization", f"{token.token_type} {token.token}")


class AsyncBearerTokenPolicy(AsyncPolicy):
    """Async twin of ``BearerTokenPolicy``.

    Concurrent refreshes are serialized via an ``asyncio.Lock`` using a
    double-checked pattern; the underlying ``AsyncTokenCredential`` is
    invoked at most once per refresh window even when many tasks call
    ``send`` concurrently.

    Like the sync policy, an optional ``challenge_handler`` plugs in a
    scheme-aware handler (e.g. ``DigestChallengeHandler``); it is consulted
    before ``on_challenge`` on 401/407 and, if it satisfies the challenge,
    the returned ``(name, value)`` pair is stamped on the retried request.
    A 401 invalidates the cached origin token; a 407 leaves it alone because
    the proxy, not the origin, rejected the request.
    """

    STAGE = Stage.AUTH
    __slots__ = (
        "_audience",
        "_cache",
        "_challenge_handler",
        "_clock",
        "_credential",
        "_lock",
        "_scopes",
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
        # ``AsyncClock`` is forwarded to the sync ``needs_refresh`` helper:
        # only ``now()`` is consulted there (which is sync on both Clock
        # variants), so the protocol mismatch on ``sleep`` is irrelevant.
        self._clock: AsyncClock = clock if clock is not None else ASYNC_SYSTEM_CLOCK
        self._lock = asyncio.Lock()
        self._challenge_handler = challenge_handler

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        request = await self._authorize(request, ctx)
        response = await self.next.send(request, ctx)
        status = int(response.status)
        if status not in (401, 407):
            return response
        # On 401 the bearer token was rejected: drop it from the cache so the
        # ``on_challenge`` fallback path forces a refresh. A 407 means the
        # proxy rejected us, not the origin — leave the cached token alone.
        if status == 401:
            self._cache.set(self._scopes, _expired_token(), self._audience)
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
        if "WWW-Authenticate" in response.headers and await self.on_challenge(request, response):
            request = await self._authorize(request, ctx, force_refresh=True)
            # Release the rejected 401 before retrying with the refreshed token.
            await response.close()
            response = await self.next.send(request, ctx)
            if int(response.status) != 401:
                return response
        raise ClientAuthenticationError(response=response)

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
            By the time this hook runs, ``send`` has already replaced the
            cached token for ``(scopes, audience)`` with an expired sentinel
            (``token=""``, ``expires_on=0``). Subclasses that inspect the
            ``TokenCache`` here will see that sentinel, not the original
            token that was attached to ``request``. The sentinel is what
            forces the subsequent ``_authorize`` call (when this hook
            returns ``True``) to invoke the credential and acquire a fresh
            token rather than reusing the rejected one.
        """
        del request, response
        return False

    async def _authorize(
        self,
        request: Request,
        ctx: PipelineContext,
        *,
        force_refresh: bool = False,
    ) -> Request:
        if ctx.options.get("enforce_https", True) and not _is_https(request.url):
            raise ServiceRequestError(
                "Bearer token authentication is not permitted for non-HTTPS URLs."
            )
        token = self._cache.get(self._scopes, self._audience)
        if force_refresh or token is None or token.needs_refresh(clock=self._clock):
            async with self._lock:
                # Double-checked: another task may have refreshed while we waited.
                token = self._cache.get(self._scopes, self._audience)
                if force_refresh or token is None or token.needs_refresh(clock=self._clock):
                    options = _token_options(ctx.options)
                    token = await self._credential.get_token_info(*self._scopes, options=options)
                    self._cache.set(self._scopes, token, self._audience)
        assert token is not None  # Narrowed by the refresh branch above.
        return request.with_header("Authorization", f"{token.token_type} {token.token}")


def _is_https(url: Url) -> bool:
    """Return True if ``url``'s scheme is ``https`` (case-insensitive)."""
    return url.scheme.lower() == "https"


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
