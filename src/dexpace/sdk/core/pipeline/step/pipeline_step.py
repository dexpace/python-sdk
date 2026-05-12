""":class:`PipelineStep` and shape-specialised aliases."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ...http.context import DispatchContext, ExchangeContext
    from ...http.request import Request
    from ...http.response import Response


@runtime_checkable
class PipelineStep[T_in, T_out](Protocol):
    """A single executable step in a pipeline workflow.

    Steps compose into chains: each takes the upstream's output plus the
    per-call :class:`DispatchContext` and emits the next stage's input.

    Implementations can be classes, lambdas, or any callable with a matching
    signature — the Protocol is structural.
    """

    def __call__(self, value: T_in, context: DispatchContext) -> T_out: ...


@runtime_checkable
class RetryableStep[T_in, T_out](Protocol):
    """:class:`PipelineStep` that exposes a retry hook.

    The retry entry receives the richer :class:`ExchangeContext` (post-dispatch,
    with the in-flight exchange's mutable state), so it can read attempt count,
    last failure, and timing without re-threading state through the primary
    call path.
    """

    def __call__(self, value: T_in, context: DispatchContext) -> T_out: ...

    def retry(self, context: ExchangeContext) -> T_out: ...


# Shape-specialised aliases. These are documentation conveniences — prefer
# annotating with the parametrised form ``PipelineStep[Request, Request]`` when
# you need to pin both input and output types.
if TYPE_CHECKING:
    type RequestPipelineStep = PipelineStep[Request, Request]
    type ResponsePipelineStep = PipelineStep[Response, Response]
else:
    RequestPipelineStep = PipelineStep
    ResponsePipelineStep = PipelineStep


__all__ = [
    "PipelineStep",
    "RequestPipelineStep",
    "ResponsePipelineStep",
    "RetryableStep",
]
