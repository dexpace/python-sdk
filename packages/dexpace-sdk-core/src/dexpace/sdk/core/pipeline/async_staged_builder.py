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
    from collections.abc import Iterator

    from ..client.async_http_client import AsyncHttpClient


class AsyncStagedPipelineBuilder:
    """Build an `AsyncPipeline` by stage rather than user-specified order.

    See `StagedPipelineBuilder` for the full behaviour — pillar single-occupancy
    enforced on every mutation path, no relocation of shipped singletons, and
    the all-or-nothing ``batch()`` transaction; this is the async counterpart.

    Thread-safety: not thread-safe. Construct on one thread, then call
    `build` — the resulting `AsyncPipeline` is independent.
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
        """Replace the first instance of ``target`` with ``new``.

        Raises:
            ValueError: If no instance of ``target`` exists in the builder, or
                if ``target`` occupies a pillar (singleton) stage and ``new``
                declares a different stage — a shipped singleton owns its stage
                and may not be relocated.
        """
        pillar_stage = next(
            (stage for stage, pillar in self._pillars.items() if isinstance(pillar, target)),
            None,
        )
        if pillar_stage is not None:
            if pillar_stage != new.STAGE:
                raise ValueError(
                    f"Cannot relocate the {pillar_stage.name} singleton: "
                    f"replacement {type(new).__name__} declares stage "
                    f"{new.STAGE.name}. A shipped singleton owns its stage; "
                    f"pass a replacement declared at {pillar_stage.name}."
                )
            # Same-stage swap. The lookup above finished iterating before we
            # mutate ``_pillars``; ``force`` overwrites the vacated slot.
            del self._pillars[pillar_stage]
            self.append(new, force=True)
            return self
        for stage, bucket in list(self._buckets.items()):
            for i, p in enumerate(bucket):
                if isinstance(p, target):
                    if stage == new.STAGE:
                        bucket[i] = new
                    else:
                        # Place ``new`` first so a rejected pillar occupancy
                        # leaves the incumbent intact — no half-applied swap.
                        self.append(new)
                        del bucket[i]
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

    @contextlib.contextmanager
    def batch(self) -> Iterator[Self]:
        """Apply a group of edits as an all-or-nothing transaction.

        Yields ``self`` so edits chain naturally inside the ``with`` block. If
        any edit raises, the builder rolls back to its pre-batch state, so a
        failing batch leaves no partial mutation visible and any pipeline built
        beforehand is untouched.

        Yields:
            This builder, for chaining edits inside the ``with`` block.
        """
        saved_pillars = dict(self._pillars)
        saved_buckets = {stage: list(bucket) for stage, bucket in self._buckets.items()}
        try:
            yield self
        except Exception:
            self._pillars = saved_pillars
            self._buckets = saved_buckets
            raise

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
        """Rebuild the stage buckets from a flat policy list, atomically.

        Validates single-occupancy of every pillar stage across the whole list
        before touching any builder state, so a list that would double-fill a
        pillar raises without leaving the builder half-reloaded.

        Args:
            policies: The full, ordered policy list to re-bucket.

        Raises:
            ValueError: If two policies claim the same pillar stage. The error
                names the stage and the incumbent policy type.
        """
        pillars: dict[Stage, AsyncPolicy] = {}
        buckets: dict[Stage, list[AsyncPolicy]] = {}
        for p in policies:
            stage = p.STAGE
            if not stage.is_pillar:
                buckets.setdefault(stage, []).append(p)
            elif stage in pillars:
                raise ValueError(
                    f"Pillar stage {stage.name} is already filled by "
                    f"{type(pillars[stage]).__name__}; it cannot also admit "
                    f"{type(p).__name__}."
                )
            else:
                pillars[stage] = p
        self._pillars = pillars
        self._buckets = buckets


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
