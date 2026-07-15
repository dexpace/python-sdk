# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Stage-based builder for ``Pipeline``.

Alternative to the dead-simple ``Pipeline(client, policies=[...])`` list
constructor. Policies declare their ``STAGE``; the builder slots them into
stage buckets and at ``build()`` time flattens to a list in stage order.

Pillar stages (`OPERATION`, `REDIRECT`, `RETRY`, `AUTH`, `LOGGING`, `SERDE`)
admit at most one policy — the singleton guarantee is enforced on *every*
mutation path (append, prepend, replace, insert, bulk reload), not just
``append``. A second occupant raises a descriptive error naming the stage and
the incumbent policy type; use ``replace(target, new)`` for explicit swaps or
``append(p, force=True)`` for the rare legitimate use case (test fixtures,
runtime composition). Shipped singletons own their stage and cannot be
relocated to a different one. Group several edits with ``batch()`` for
all-or-nothing application.

Re-installing the *same* policy instance onto its already-occupied pillar is
an idempotent no-op: reference identity, not value equality, distinguishes
"same" from "distinct". A second *distinct* occupant still raises, but
re-adding the incumbent object simply returns without error or duplication.

Batch adds come in two flavours with a deliberate ordering asymmetry.
``append_all(policies)`` preserves the batch's iteration order within each
stage (``[a, b, c]`` stays ``[a, b, c]``); ``prepend_all(policies)`` prepends
each element individually and therefore REVERSES it (``[a, b, c]`` becomes
``[c, b, a]``, because ``c`` is prepended last and so ends up first). Both are
all-or-nothing.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Self

from .pipeline import Pipeline
from .policy import Policy
from .stage import Stage

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

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

    def append_all(self, policies: Iterable[Policy], *, force: bool = False) -> Self:
        """Append every policy in ``policies``, preserving iteration order.

        Each element is appended in turn, so the batch keeps its order within
        its stage: appending ``[a, b, c]`` yields ``[a, b, c]``. This is the
        deliberate mirror image of `prepend_all`, which reverses the batch —
        see that method for the rationale behind the asymmetry.

        The batch is all-or-nothing: if any element is rejected (for example a
        second distinct occupant for a filled pillar) the builder rolls back to
        its pre-call state and no element is left applied.

        Args:
            policies: Policies to append, consumed in iteration order.
            force: Forwarded to `append` for pillar overwrites.

        Returns:
            ``self`` for chaining.

        Raises:
            ValueError: If any element cannot be added (e.g. it double-fills a
                pillar); the builder is left unchanged.
        """
        with self.batch() as staged:
            for policy in policies:
                staged.append(policy, force=force)
        return self

    def prepend_all(self, policies: Iterable[Policy], *, force: bool = False) -> Self:
        """Prepend every policy in ``policies`` one at a time, reversing order.

        Because each element is prepended to the head in turn, the batch's
        order is REVERSED within its stage: prepending ``[a, b, c]`` yields
        ``[c, b, a]`` — ``c`` is prepended last, so it ends up first. This
        asymmetry with `append_all` (which preserves order) is intentional and
        matches prepending elements individually; callers who want the batch to
        keep its order should reverse the input or use `append_all`.

        The batch is all-or-nothing, exactly like `append_all`.

        Args:
            policies: Policies to prepend, consumed in iteration order.
            force: Forwarded to `prepend` for pillar overwrites.

        Returns:
            ``self`` for chaining.

        Raises:
            ValueError: If any element cannot be added (e.g. it double-fills a
                pillar); the builder is left unchanged.
        """
        with self.batch() as staged:
            for policy in policies:
                staged.prepend(policy, force=force)
        return self

    def replace(self, target: type[Policy], new: Policy) -> Self:
        """Replace the first instance of ``target`` with ``new``.

        The replacement is by-type, so ``replace(RetryPolicy, custom)``
        swaps the existing retry policy regardless of whether it sits in
        the pillar slot or a non-pillar bucket.

        Args:
            target: Type whose first matching instance is replaced.
            new: Replacement policy. Must declare a ``STAGE``. When ``target``
                occupies a pillar (singleton) stage, ``new`` must declare that
                same stage; for non-pillar targets a cross-stage swap is
                allowed and re-buckets ``new`` by its declared stage.

        Returns:
            ``self`` for chaining.

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
    def from_pipeline(cls, pipeline: Pipeline) -> Self:
        """Seed a builder from an existing `Pipeline`'s policies.

        Walks the pipeline's policy chain (skipping the internal transport
        runner) and re-slots each policy into its declared stage. Useful
        for "build a default pipeline, then surgically swap one piece"
        workflows.

        The harvested policy instances are detached from ``pipeline`` (their
        ``.next`` links are cleared) so they can be re-wired into the rebuilt
        pipeline. ``pipeline`` is consumed by this call — each policy is owned
        by a single pipeline, so the source pipeline must not be run again.

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
            _detach(policy)
            builder.append(policy, force=True)
        return builder

    def _install_pillar(self, policy: Policy, stage: Stage, *, force: bool) -> None:
        incumbent = self._pillars.get(stage)
        if incumbent is policy:
            # Re-installing the SAME instance is an idempotent no-op: reference
            # identity, not value equality, tells 'same' from 'distinct'.
            return
        if incumbent is not None and not force:
            existing = type(incumbent).__name__
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
        pillars: dict[Stage, Policy] = {}
        buckets: dict[Stage, list[Policy]] = {}
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


def _detach(policy: Policy) -> None:
    """Clear ``policy.next`` so the instance can be re-wired into a new chain.

    A policy harvested from an existing pipeline still points at that
    pipeline's chain. Clearing the link makes it look freshly constructed to
    ``Pipeline``'s single-ownership guard, allowing the rebuild to re-wire it.

    Args:
        policy: The policy whose ``.next`` link is removed if present.
    """
    with contextlib.suppress(AttributeError):
        del policy.next


__all__ = ["StagedPipelineBuilder"]
