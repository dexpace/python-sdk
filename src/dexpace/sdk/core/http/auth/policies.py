"""Built-in authentication pipeline policies."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from ...errors import ClientAuthenticationError, ServiceRequestError
from ...pipeline.async_policy import AsyncPolicy
from ...pipeline.policy import Policy
from .access_token import AccessTokenInfo, TokenRequestOptions
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
    to handle CAE/claims challenges.
    """

    __slots__ = ("_audience", "_cache", "_credential", "_lock", "_scopes")

    def __init__(
        self,
        credential: TokenCredential,
        *scopes: str,
        cache: TokenCache | None = None,
        audience: str | None = None,
    ) -> None:
        if not scopes:
            raise ValueError("at least one scope is required")
        self._credential = credential
        self._scopes = scopes
        self._cache: TokenCache = cache or InMemoryTokenCache()
        self._audience = audience
        self._lock = threading.Lock()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        request = self._authorize(request, ctx)
        response = self.next.send(request, ctx)
        if int(response.status) != 401:
            return response
        # Drop cached token and ask subclasses to handle the challenge.
        self._cache.set(self._scopes, _expired_token(), self._audience)
        if "WWW-Authenticate" in response.headers and self.on_challenge(request, response):
            request = self._authorize(request, ctx, force_refresh=True)
            response = self.next.send(request, ctx)
            if int(response.status) != 401:
                return response
        raise ClientAuthenticationError(response=response)

    def on_challenge(self, request: Request, response: Response) -> bool:
        """Override to handle a ``WWW-Authenticate`` challenge.

        Args:
            request: The request that produced the 401.
            response: The 401 response.

        Returns:
            ``True`` to re-issue the request after acquiring a new token;
            ``False`` (the default) to surface the error to the caller.
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
        if force_refresh or token is None or token.needs_refresh():
            with self._lock:
                # Double-checked: another thread may have refreshed while we waited.
                token = self._cache.get(self._scopes, self._audience)
                if force_refresh or token is None or token.needs_refresh():
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
    """

    __slots__ = ("_audience", "_cache", "_credential", "_lock", "_scopes")

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *scopes: str,
        cache: TokenCache | None = None,
        audience: str | None = None,
    ) -> None:
        if not scopes:
            raise ValueError("at least one scope is required")
        self._credential = credential
        self._scopes = scopes
        self._cache: TokenCache = cache or InMemoryTokenCache()
        self._audience = audience
        self._lock = asyncio.Lock()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        request = await self._authorize(request, ctx)
        response = await self.next.send(request, ctx)
        if int(response.status) != 401:
            return response
        self._cache.set(self._scopes, _expired_token(), self._audience)
        if "WWW-Authenticate" in response.headers and await self.on_challenge(request, response):
            request = await self._authorize(request, ctx, force_refresh=True)
            response = await self.next.send(request, ctx)
            if int(response.status) != 401:
                return response
        raise ClientAuthenticationError(response=response)

    async def on_challenge(
        self,
        request: Request,
        response: AsyncResponse,
    ) -> bool:
        """Async version of ``BearerTokenPolicy.on_challenge``."""
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
        if force_refresh or token is None or token.needs_refresh():
            async with self._lock:
                # Double-checked: another task may have refreshed while we waited.
                token = self._cache.get(self._scopes, self._audience)
                if force_refresh or token is None or token.needs_refresh():
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
