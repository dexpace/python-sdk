# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Stage-based builder for ``Pipeline``.

Alternative to the dead-simple ``Pipeline(client, policies=[...])`` list
constructor. Policies declare their ``STAGE``; the builder slots them into
stage buckets and at ``build()`` time flattens to a list in stage order.

Pillar stages (`REDIRECT`, `RETRY`, `AUTH`, `LOGGING`, `SERDE`) admit at
most one policy. A second `append` of a pillar raises by default — use
``replace(target, new)`` for explicit swaps or ``append(p, force=True)``
for the rare legitimate use case (test fixtures, runtime composition).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from .pipeline import Pipeline
from .policy import Policy
from .stage import Stage

if TYPE_CHECKING:
    from ..client.http_client import HttpClient


class StagedPipelineBuilder:
    """Build a `Pipeline` by stage rather than user-specified order.

    Surgical edits (``replace`` / ``insert_after`` / ``insert_before`` /
    ``remove``) take a policy *type* and operate on the first matching
    instance — useful for tweaking an already-built pipeline obtained via
    `from_pipeline`.

    Thread-safety: not thread-safe. Construct on one thread, then call
    `build` — the resulting `Pipeline` is independent.
    """

    __slots__ = ("_buckets", "_client", "_pillars")

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._pillars: dict[Stage, Policy] = {}
        self._buckets: dict[Stage, list[Policy]] = {}

    def append(self, policy: Policy, *, force: bool = False) -> Self:
        """Append ``policy`` to the tail of its stage's bucket.

        Args:
            policy: Concrete policy declaring ``STAGE: ClassVar[Stage]``.
            force: When the policy's stage is a pillar and another policy
                already occupies it, ``force=True`` silently overwrites.
                Default is to raise ``ValueError`` so accidental double-fills
                surface immediately.

        Returns:
            ``self`` for chaining.

        Raises:
            ValueError: When ``policy.STAGE`` is a pillar already filled
                and ``force`` is False.
        """
        stage = policy.STAGE
        if stage.is_pillar:
            self._install_pillar(policy, stage, force=force)
        else:
            self._buckets.setdefault(stage, []).append(policy)
        return self

    def prepend(self, policy: Policy, *, force: bool = False) -> Self:
        """Prepend ``policy`` to the head of its stage's bucket.

        Pillar behaviour mirrors `append`.
        """
        stage = policy.STAGE
        if stage.is_pillar:
            self._install_pillar(policy, stage, force=force)
        else:
            self._buckets.setdefault(stage, []).insert(0, policy)
        return self

    def replace(self, target: type[Policy], new: Policy) -> Self:
        """Replace the first instance of ``target`` with ``new``.

        The replacement is by-type, so ``replace(RetryPolicy, custom)``
        swaps the existing retry policy regardless of whether it sits in
        the pillar slot or a non-pillar bucket.

        Args:
            target: Type whose first matching instance is replaced.
            new: Replacement policy. Must declare a ``STAGE`` — typically
                the same stage as ``target`` but not enforced.

        Returns:
            ``self`` for chaining.

        Raises:
            ValueError: If no instance of ``target`` exists in the builder.
        """
        for stage, pillar in self._pillars.items():
            if isinstance(pillar, target):
                # Install new at its declared stage; remove the old pillar.
                del self._pillars[stage]
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

    def insert_after(self, target: type[Policy], new: Policy) -> Self:
        """Insert ``new`` immediately after the first ``target`` instance."""
        return self._splice(target, new, offset=1)

    def insert_before(self, target: type[Policy], new: Policy) -> Self:
        """Insert ``new`` immediately before the first ``target`` instance."""
        return self._splice(target, new, offset=0)

    def remove(self, target: type[Policy]) -> Self:
        """Remove every instance of ``target`` from the builder. No-op if absent."""
        self._pillars = {s: p for s, p in self._pillars.items() if not isinstance(p, target)}
        for stage in list(self._buckets):
            self._buckets[stage] = [p for p in self._buckets[stage] if not isinstance(p, target)]
            if not self._buckets[stage]:
                del self._buckets[stage]
        return self

    def build(self) -> Pipeline:
        """Flatten the builder's contents into a `Pipeline`."""
        return Pipeline(self._client, policies=self._flatten())

    @classmethod
    def from_pipeline(cls, pipeline: Pipeline) -> Self:
        """Seed a builder from an existing `Pipeline`'s policies.

        Walks the pipeline's policy chain (skipping the internal transport
        runner) and re-slots each policy into its declared stage. Useful
        for "build a default pipeline, then surgically swap one piece"
        workflows.

        Raises:
            ValueError: If the input pipeline's policies do not satisfy
                stage ordering — i.e. their declared stages do not appear
                in non-decreasing order in the chain — or if the chain
                contains a list-constructor SansIO step (a bare callable),
                which carries no ``STAGE`` and so cannot be rehydrated.
        """
        from ._transport_runner import _TransportRunner

        builder = cls(pipeline.transport)
        chain: list[Policy] = []
        node: Policy | None = pipeline._chain
        while node is not None and not isinstance(node, _TransportRunner):
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
                    f"Pipeline policy {type(policy).__name__} at stage {policy.STAGE} "
                    f"comes after stage {last_stage}; staged builder requires "
                    f"non-decreasing stage order. Use the list constructor instead."
                )
            last_stage = policy.STAGE
            builder.append(policy, force=True)
        return builder

    def _install_pillar(self, policy: Policy, stage: Stage, *, force: bool) -> None:
        if stage in self._pillars and not force:
            existing = type(self._pillars[stage]).__name__
            raise ValueError(
                f"Pillar stage {stage.name} is already filled by {existing}. "
                f"Use replace({type(policy).__name__}, new) to swap, or "
                f"force=True to overwrite."
            )
        self._pillars[stage] = policy

    def _splice(self, target: type[Policy], new: Policy, *, offset: int) -> Self:
        # Splice operates on the flattened order; the result is re-bucketed.
        flat = self._flatten()
        for i, p in enumerate(flat):
            if isinstance(p, target):
                flat.insert(i + offset, new)
                self._reload(flat)
                return self
        raise ValueError(f"No instance of {target.__name__} in the builder.")

    def _flatten(self) -> list[Policy]:
        out: list[Policy] = []
        for stage in Stage:
            if stage is Stage.SEND:
                continue  # terminal — reserved for the transport
            if stage.is_pillar:
                if stage in self._pillars:
                    out.append(self._pillars[stage])
            else:
                out.extend(self._buckets.get(stage, ()))
        return out

    def _reload(self, policies: list[Policy]) -> None:
        self._pillars.clear()
        self._buckets.clear()
        for p in policies:
            if p.STAGE.is_pillar:
                self._pillars[p.STAGE] = p
            else:
                self._buckets.setdefault(p.STAGE, []).append(p)


__all__ = ["StagedPipelineBuilder"]
