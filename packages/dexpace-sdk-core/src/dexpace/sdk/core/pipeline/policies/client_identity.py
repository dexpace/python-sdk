# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline policy that stamps each request with an identifying ``User-Agent``."""

from __future__ import annotations

import platform
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from ...http.common.http_header_name import USER_AGENT
from ..policy import Policy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.response import Response
    from ..context import PipelineContext

_DIST_NAME: Final[str] = "dexpace-sdk-core"
_FALLBACK_VERSION: Final[str] = "0.0.0"


def _sdk_version() -> str:
    """Return the installed core distribution version, or a safe fallback.

    The version is read from installed package metadata via
    ``importlib.metadata``. When the distribution is not installed (for
    example, running from a source tree without an editable install), a
    placeholder is returned rather than raising, so the policy never produces
    a blank or error-laden ``User-Agent``.

    Returns:
        The ``dexpace-sdk-core`` version string, or ``"0.0.0"`` if it cannot
        be resolved.
    """
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def default_user_agent() -> str:
    """Build the SDK's default ``User-Agent`` token string.

    The shape is ``dexpace-sdk/<sdk-version> python/<python-version>`` — for
    example ``dexpace-sdk/1.2.0 python/3.12.4``. Transport packages may append
    their own ``<lib>/<version>`` token by passing a longer ``user_agent`` to
    :class:`ClientIdentityPolicy`.

    Returns:
        A non-empty ``User-Agent`` string.
    """
    return f"dexpace-sdk/{_sdk_version()} python/{platform.python_version()}"


class ClientIdentityPolicy(Policy):
    """Stamps the outgoing request with an identifying ``User-Agent`` header.

    A consistent ``User-Agent`` lets servers and the SDK's own observability
    attribute traffic to the toolkit and its version. The token string defaults
    to :func:`default_user_agent` (``dexpace-sdk/<ver> python/<pyver>``).

    Two modes control interaction with a caller-set header:

    - **append** (the default): a caller-supplied ``User-Agent`` is preserved
      and this policy's token is appended after it, space-separated, so both
      identities reach the wire.
    - **replace**: any caller-supplied ``User-Agent`` is overwritten.

    The configured token is required to be non-blank, so the policy never emits
    an empty ``User-Agent`` header.

    Attributes:
        STAGE: Pinned to :attr:`Stage.POST_RETRY` at the type level so
            mis-slotting is caught by ``mypy``.

    Example:
        ```python
        Pipeline(transport, policies=[ClientIdentityPolicy()])
        ```
    """

    STAGE: ClassVar[Literal[Stage.POST_RETRY]] = Stage.POST_RETRY
    __slots__ = ("_replace", "_user_agent")

    def __init__(self, *, user_agent: str | None = None, replace: bool = False) -> None:
        """Build the policy.

        Args:
            user_agent: ``User-Agent`` token to stamp. ``None`` (the default)
                uses :func:`default_user_agent`. An empty or whitespace-only
                value is rejected so the header is never blank.
            replace: When ``True``, overwrite any caller-set ``User-Agent``.
                When ``False`` (the default), append after the caller's value.

        Raises:
            ValueError: If ``user_agent`` is provided but empty or whitespace.
        """
        resolved = default_user_agent() if user_agent is None else user_agent
        if not resolved.strip():
            raise ValueError("user_agent must be a non-empty token string")
        self._user_agent = resolved
        self._replace = replace

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        """Stamp ``request`` with the ``User-Agent`` header and dispatch.

        Args:
            request: Outgoing request. A new request is returned.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        existing = request.headers.get(USER_AGENT)
        if self._replace or not existing or not existing.strip():
            value = self._user_agent
        else:
            value = f"{existing} {self._user_agent}"
        return self.next.send(request.with_header(USER_AGENT, value), ctx)


__all__ = ["ClientIdentityPolicy", "default_user_agent"]
