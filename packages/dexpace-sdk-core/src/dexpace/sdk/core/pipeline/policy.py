"""``Policy`` ABC — pipeline steps that wrap the downstream chain."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from .stage import Stage

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.response import Response
    from .context import PipelineContext


class Policy(ABC):
    """Pipeline step that can decide whether (and how) to invoke ``self.next``.

    Use ``Policy`` when a step needs to wrap the downstream chain — for
    retry, authentication challenges, span lifecycles, etc. Stateless,
    transport-agnostic transforms should be a ``PipelineStep`` Protocol
    instead; the pipeline runner wraps those in an internal SansIO runner
    that calls ``self.next`` after the transform.

    Modelled on Azure's ``corehttp.runtime.policies.HTTPPolicy``: ``.next``
    is a per-instance attribute wired up by the pipeline constructor; the
    terminal node is a transport runner. Subclasses implement ``send``.

    Concrete subclasses must declare ``STAGE: ClassVar[Stage]`` so
    ``StagedPipelineBuilder`` knows where to slot them. The list-form
    ``Pipeline(client, policies=[...])`` constructor ignores ``STAGE`` —
    declaring it is still required for consistency. Enforcement happens at
    class-creation time via ``__init_subclass__``: a concrete subclass
    missing ``STAGE`` raises ``TypeError`` on import.
    """

    STAGE: ClassVar[Stage]
    next: Policy

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Pipeline-internal adapters (leading-underscore names) are slotted
        # by the pipeline runner itself, not by the staged builder.
        if cls.__name__.startswith("_"):
            return
        # Abstract intermediates (still carrying abstract methods) may skip
        # STAGE. ``__abstractmethods__`` is not yet populated when
        # ``__init_subclass__`` runs (ABCMeta sets it later), so we walk the
        # attributes ourselves looking for ``__isabstractmethod__``.
        if any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False) for name in dir(cls)
        ):
            return
        if "STAGE" not in cls.__dict__ and not any(
            "STAGE" in base.__dict__ for base in cls.__mro__[1:] if base is not Policy
        ):
            raise TypeError(
                f"{cls.__name__} must declare STAGE: ClassVar[Stage]. "
                f"See dexpace.sdk.core.pipeline.stage.Stage for choices."
            )

    @abstractmethod
    def send(self, request: Request, ctx: PipelineContext) -> Response:
        """Process ``request`` and return its response.

        Implementations typically mutate the request, call
        ``self.next.send(request, ctx)``, and post-process the response (or
        loop, in the retry case).

        Args:
            request: The HTTP request to process.
            ctx: Mutable pipeline state for this exchange.

        Returns:
            The response from the downstream chain.
        """


__all__ = ["Policy"]
