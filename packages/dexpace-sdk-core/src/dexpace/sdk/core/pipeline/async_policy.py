"""Async twin of ``Policy``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from .stage import Stage

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from .context import PipelineContext


class AsyncPolicy(ABC):
    """Async pipeline step that can decide whether (and how) to invoke ``self.next``.

    Mirrors ``Policy`` but with ``async def send`` and ``AsyncResponse``
    semantics. ``.next`` is wired up by ``AsyncPipeline`` at construction.

    Concrete subclasses must declare ``STAGE: ClassVar[Stage]``; the
    enforcement and rationale mirror ``Policy.__init_subclass__``.
    """

    STAGE: ClassVar[Stage]
    next: AsyncPolicy

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Pipeline-internal adapters (leading-underscore names) are slotted
        # by the pipeline runner itself, not by the staged builder.
        if cls.__name__.startswith("_"):
            return
        # Abstract intermediates (still carrying abstract methods) may skip
        # STAGE. ``__abstractmethods__`` is not yet populated when
        # ``__init_subclass__`` runs (ABCMeta sets it later).
        if any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False) for name in dir(cls)
        ):
            return
        if "STAGE" not in cls.__dict__ and not any(
            "STAGE" in base.__dict__ for base in cls.__mro__[1:] if base is not AsyncPolicy
        ):
            raise TypeError(
                f"{cls.__name__} must declare STAGE: ClassVar[Stage]. "
                f"See dexpace.sdk.core.pipeline.stage.Stage for choices."
            )

    @abstractmethod
    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Process ``request`` and return its async response."""


__all__ = ["AsyncPolicy"]
