# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that stamps write requests with a stable ``Idempotency-Key``."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Final, Literal
from uuid import uuid4

from ...http.request.method import Method
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext

_DEFAULT_HEADER: Final[str] = "Idempotency-Key"
_DEFAULT_METHODS: Final[frozenset[Method]] = frozenset({Method.POST, Method.PUT, Method.PATCH})


def _generate_key() -> str:
    """Return a fresh random idempotency key (a UUID4 string)."""
    return str(uuid4())


class IdempotencyPolicy(Policy):
    """Adds an ``Idempotency-Key`` header to write requests.

    The key is generated **once**, before the request is dispatched, and the
    same value is carried across every retry of that request. This lets a
    server detect a retried ``POST``/``PUT``/``PATCH`` as a duplicate of an
    earlier attempt and avoid processing it twice — turning an at-least-once
    delivery into an effectively-exactly-once one.

    A caller-supplied header is left untouched: if the request already carries
    the configured header, this policy does nothing. Only the configured
    methods (``POST``/``PUT``/``PATCH`` by default) are stamped; idempotency
    keys on ``GET``/``DELETE`` are meaningless to most servers.

    The policy is placed at `Stage.POST_REDIRECT`, which runs *outside*
    the retry wrapper (`Stage.RETRY`). The key is therefore minted on the
    first pass and reused on every retry re-send, rather than re-rolled per
    attempt the way `SetDatePolicy` re-stamps the ``Date`` header.

    Attributes:
        STAGE: Pinned to `Stage.POST_REDIRECT` at the type level so
            mis-slotting is caught by ``mypy``.

    Example:
        ```python
        Pipeline(transport, policies=[RedirectPolicy(), IdempotencyPolicy(), RetryPolicy()])
        ```
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
                ``POST``/``PUT``/``PATCH`` — the standard non-idempotent write
                verbs.
            header: Header name carrying the key. Defaults to
                ``Idempotency-Key`` (the Stripe / IETF draft spelling).
            key_factory: Zero-argument callable returning a fresh key string.
                Defaults to a UUID4 generator; tests inject a deterministic
                stub.
        """
        self._methods = frozenset(methods)
        self._header = header
        self._key_factory = key_factory

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        """Stamp ``request`` with an idempotency key when applicable and dispatch.

        Args:
            request: Outgoing request. A new request carrying the key is
                returned when one is added; otherwise the request is forwarded
                unchanged.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        if request.method in self._methods and self._header not in request.headers:
            request = request.with_header(self._header, self._key_factory())
        return self.next.send(request, ctx)


__all__ = ["IdempotencyPolicy"]
