# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of `StagedPipelineBuilder`.

Behaviour mirrors the sync builder exactly: pillar enforcement, surgical
edits, ``from_pipeline`` round-trip. The only differences are the policy
types (``AsyncPolicy`` instead of ``Policy``) and the produced pipeline
(``AsyncPipeline`` instead of ``Pipeline``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Self

from .async_pipeline import AsyncPipeline
from .async_policy import AsyncPolicy
from .stage import Stage

if TYPE_CHECKING:
    from ..client.async_http_client import AsyncHttpClient


class AsyncStagedPipelineBuilder:
    """Build an `AsyncPipeline` by stage rather than user-specified order.

    See `StagedPipelineBuilder` for the full behaviour; this is the
    async counterpart.
    """

    __slots__ = ("_buckets", "_client", "_pillars")

    def __init__(self, client: AsyncHttpClient) -> None:
        self._client = client
        self._pillars: dict[Stage, AsyncPolicy] = {}
        self._buckets: dict[Stage, list[AsyncPolicy]] = {}

    def append(self, policy: AsyncPolicy, *, force: bool = False) -> Self:
        """Append ``policy`` to the tail of its stage's bucket."""
        stage = policy.STAGE
        if stage.is_pillar:
            self._install_pillar(policy, stage, force=force)
        else:
            self._buckets.setdefault(stage, []).append(policy)
        return self

    def prepend(self, policy: AsyncPolicy, *, force: bool = False) -> Self:
        """Prepend ``policy`` to the head of its stage's bucket."""
        stage = policy.STAGE
        if stage.is_pillar:
            self._install_pillar(policy, stage, force=force)
        else:
            self._buckets.setdefault(stage, []).insert(0, policy)
        return self

    def replace(self, target: type[AsyncPolicy], new: AsyncPolicy) -> Self:
        """Replace the first instance of ``target`` with ``new``."""
        pillar_stage = next(
            (stage for stage, pillar in self._pillars.items() if isinstance(pillar, target)),
            None,
        )
        if pillar_stage is not None:
            # The lookup above finished iterating before we mutate ``_pillars``.
            del self._pillars[pillar_stage]
            self.append(new, force=True)
            return self
        for stage, bucket in self._buckets.items():
            for i, p in enumerate(bucket):
                if isinstance(p, target):
                    if stage == new.STAGE:
                        bucket[i] = new
                    else:
                        del bucket[i]
                        self.append(new)
                    return self
        raise ValueError(f"No instance of {target.__name__} in the builder.")

    def insert_after(self, target: type[AsyncPolicy], new: AsyncPolicy) -> Self:
        """Insert ``new`` immediately after the first ``target`` instance."""
        return self._splice(target, new, offset=1)

    def insert_before(self, target: type[AsyncPolicy], new: AsyncPolicy) -> Self:
        """Insert ``new`` immediately before the first ``target`` instance."""
        return self._splice(target, new, offset=0)

    def remove(self, target: type[AsyncPolicy]) -> Self:
        """Remove every instance of ``target`` from the builder."""
        self._pillars = {s: p for s, p in self._pillars.items() if not isinstance(p, target)}
        for stage in list(self._buckets):
            self._buckets[stage] = [p for p in self._buckets[stage] if not isinstance(p, target)]
            if not self._buckets[stage]:
                del self._buckets[stage]
        return self

    def build(self) -> AsyncPipeline:
        """Flatten the builder's contents into an `AsyncPipeline`."""
        return AsyncPipeline(self._client, policies=self._flatten())

    @classmethod
    def from_pipeline(cls, pipeline: AsyncPipeline) -> Self:
        """Seed a builder from an existing `AsyncPipeline`.

        The harvested policy instances are detached from ``pipeline`` (their
        ``.next`` links are cleared) so they can be re-wired into the rebuilt
        pipeline. ``pipeline`` is consumed by this call — each policy is owned
        by a single pipeline, so the source pipeline must not be run again.

        Raises:
            ValueError: If the input pipeline's policies do not satisfy
                stage ordering, or if the chain contains a list-constructor
                SansIO step (a bare callable), which carries no ``STAGE``
                and so cannot be rehydrated.
        """
        from ._async_transport_runner import _AsyncTransportRunner

        builder = cls(pipeline.transport)
        chain: list[AsyncPolicy] = []
        node: AsyncPolicy | None = pipeline._chain
        while node is not None and not isinstance(node, _AsyncTransportRunner):
            if getattr(node, "STAGE", None) is None:
                raise ValueError(
                    f"Pipeline node {type(node).__name__} carries no STAGE; "
                    f"it is a list-constructor SansIO step (a bare callable) "
                    f"that cannot be rehydrated into a staged builder. Rebuild "
                    f"the pipeline via the list constructor instead."
                )
            chain.append(node)
            node = getattr(node, "next", None)
        last_stage: Stage | None = None
        for policy in chain:
            if last_stage is not None and last_stage > policy.STAGE:
                raise ValueError(
                    f"AsyncPipeline policy {type(policy).__name__} at stage {policy.STAGE} "
                    f"comes after stage {last_stage}; staged builder requires "
                    f"non-decreasing stage order. Use the list constructor instead."
                )
            last_stage = policy.STAGE
            _detach(policy)
            builder.append(policy, force=True)
        return builder

    def _install_pillar(self, policy: AsyncPolicy, stage: Stage, *, force: bool) -> None:
        if stage in self._pillars and not force:
            existing = type(self._pillars[stage]).__name__
            raise ValueError(
                f"Pillar stage {stage.name} is already filled by {existing}. "
                f"Use replace({type(policy).__name__}, new) to swap, or "
                f"force=True to overwrite."
            )
        self._pillars[stage] = policy

    def _splice(self, target: type[AsyncPolicy], new: AsyncPolicy, *, offset: int) -> Self:
        flat = self._flatten()
        for i, p in enumerate(flat):
            if isinstance(p, target):
                flat.insert(i + offset, new)
                self._reload(flat)
                return self
        raise ValueError(f"No instance of {target.__name__} in the builder.")

    def _flatten(self) -> list[AsyncPolicy]:
        out: list[AsyncPolicy] = []
        for stage in Stage:
            if stage is Stage.SEND:
                continue
            if stage.is_pillar:
                if stage in self._pillars:
                    out.append(self._pillars[stage])
            else:
                out.extend(self._buckets.get(stage, ()))
        return out

    def _reload(self, policies: list[AsyncPolicy]) -> None:
        self._pillars.clear()
        self._buckets.clear()
        for p in policies:
            if p.STAGE.is_pillar:
                self._pillars[p.STAGE] = p
            else:
                self._buckets.setdefault(p.STAGE, []).append(p)


def _detach(policy: AsyncPolicy) -> None:
    """Clear ``policy.next`` so the instance can be re-wired into a new chain.

    A policy harvested from an existing pipeline still points at that
    pipeline's chain. Clearing the link makes it look freshly constructed to
    ``AsyncPipeline``'s single-ownership guard, allowing the rebuild to
    re-wire it.

    Args:
        policy: The policy whose ``.next`` link is removed if present.
    """
    with contextlib.suppress(AttributeError):
        del policy.next


__all__ = ["AsyncStagedPipelineBuilder"]
