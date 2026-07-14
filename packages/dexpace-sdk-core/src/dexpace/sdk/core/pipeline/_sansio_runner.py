# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Adapter that runs a SansIO ``PipelineStep`` inside a ``Policy`` chain.

A SansIO step is a stateless ``(value, ctx) -> value`` transform — typically
header stamping, redaction, or a structured logging hook. Wrapping it in a
``Policy`` lets it participate in the linked-list chain without forcing
each step to know about ``.next``.

Returning ``None`` from a SansIO step short-circuits the chain (raises
``PipelineAbortedError``); this matches the convention used elsewhere in the
SDK for "do not forward".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import PipelineAbortedError
from ._recovery import _close_and_chain
from .policy import Policy
from .step.pipeline_step import PipelineStep

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.response import Response
    from .context import PipelineContext


class _SansIORequestRunner(Policy):
    """Wraps a request-side SansIO step inside a Policy chain."""

    __slots__ = ("_step",)

    def __init__(self, step: PipelineStep[Request, Request | None]) -> None:
        self._step = step

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        transformed = self._step(request, ctx.call)
        if transformed is None:
            raise PipelineAbortedError(
                f"Pipeline step {self._step!r} returned None; aborting chain."
            )
        return self.next.send(transformed, ctx)


class _SansIOResponseRunner(Policy):
    """Wraps a response-side SansIO step inside a Policy chain."""

    __slots__ = ("_step",)

    def __init__(self, step: PipelineStep[Response, Response | None]) -> None:
        self._step = step

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        response = self.next.send(request, ctx)
        try:
            transformed = self._step(response, ctx.call)
        except BaseException as exc:
            # Close the held response before the exception continues. This
            # catch-all is deliberately broad: a step that raises an
            # ``Exception`` folds upstream, but a ``BaseException`` — an
            # ``asyncio.CancelledError`` or ``KeyboardInterrupt`` — must
            # re-raise unconverted, never be swallowed here. A failure from the
            # close itself is chained onto ``exc`` so the original exception,
            # not the close error, is what propagates.
            _close_and_chain(response, exc)
            raise
        if transformed is None:
            response.close()
            raise PipelineAbortedError(
                f"Pipeline step {self._step!r} returned None; aborting chain."
            )
        return transformed


__all__ = ["_SansIORequestRunner", "_SansIOResponseRunner"]
