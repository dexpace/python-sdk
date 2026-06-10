# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of `IdempotencyPolicy`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Literal

from ...http.request.method import Method
from ..async_policy import AsyncPolicy
from ..stage import Stage
from .idempotency import _generate_key

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext

_DEFAULT_HEADER = "Idempotency-Key"
_DEFAULT_METHODS = frozenset({Method.POST, Method.PUT, Method.PATCH})


class AsyncIdempotencyPolicy(AsyncPolicy):
    """Async variant of `IdempotencyPolicy`.

    Behaviour mirrors the sync twin: a key is minted once at
    `Stage.POST_REDIRECT` (outside the retry wrapper) and reused across
    every retry of the same write request, and a caller-supplied header is left
    untouched. Key generation is synchronous, so `send` differs from the
    sync version only in the ``await`` on the downstream call.

    Attributes:
        STAGE: Pinned to `Stage.POST_REDIRECT` at the type level so
            mis-slotting is caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.POST_REDIRECT]] = Stage.POST_REDIRECT
    __slots__ = ("_header", "_key_factory", "_methods")

    def __init__(
        self,
        *,
        methods: Iterable[Method] = _DEFAULT_METHODS,
        header: str = _DEFAULT_HEADER,
        key_factory: Callable[[], str] = _generate_key,
    ) -> None:
        """Build the policy.

        Args:
            methods: HTTP methods whose requests receive a key. Defaults to
                ``POST``/``PUT``/``PATCH``.
            header: Header name carrying the key. Defaults to
                ``Idempotency-Key``.
            key_factory: Zero-argument callable returning a fresh key string.
                Defaults to a UUID4 generator; tests inject a deterministic
                stub.
        """
        self._methods = frozenset(methods)
        self._header = header
        self._key_factory = key_factory

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Stamp ``request`` with an idempotency key when applicable and dispatch.

        Args:
            request: Outgoing request.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        if request.method in self._methods and self._header not in request.headers:
            request = request.with_header(self._header, self._key_factory())
        return await self.next.send(request, ctx)


__all__ = ["AsyncIdempotencyPolicy"]
