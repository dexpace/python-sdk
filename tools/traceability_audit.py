# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Requirement-ID traceability audit for the dexpace SDK test suite.

This module is two things layered on top of each other:

- A small, pytest-independent core (``Requirement`` / ``RequirementIndex`` /
  ``audit`` / matrix renderers) that cross-references a *requirement index*
  against the set of requirement IDs claimed by the test suite via
  ``@pytest.mark.req("SOME-ID")`` markers. It answers two questions per run:
  which requirement is covered by which test, and which MUST-level requirement
  in a *flagged-done* subsystem has no covering test at all.
- A thin ``TraceabilityPlugin`` that collects those markers at pytest
  collection time, writes a matrix artifact (requirement ID -> covering tests)
  and — only when explicitly gated — fails the run on an uncovered MUST.

Milestone-scoped gating. The audit never fails on the whole (still-being-built)
requirement spec at once: a MUST requirement is only a gap once its
``subsystem`` is listed in ``enabled_subsystems``. Empty enabled set (the
default) means the audit reports coverage but fails nothing, so this plumbing
can land and be exercised long before the full spec index exists.

Deviations seam. A future deviations ledger (``docs/deviations.md``, produced
by a separate task) will list requirement IDs that are intentionally not
implemented (N/A). The audit accepts that set via ``na_ids`` so those IDs never
count as gaps. ``na_ids_from_file`` is the placeholder reader; it returns an
empty set when the ledger file does not yet exist.

Placeholder index. ``PLACEHOLDER_INDEX`` is a deliberately small, representative
sample (see its own comment) — *not* the real spec. It exists to prove the
machinery end-to-end. Swap it for the real index once that index exists; the
audit and plugin are written against the generic ``RequirementIndex`` type and
need no change.

Environment overrides consumed by ``build_plugin_from_env``:

- ``DEXPACE_TRACEABILITY_MATRIX``: output path for the matrix artifact.
- ``DEXPACE_TRACEABILITY_ENABLE``: comma-separated subsystems to gate on.
- ``DEXPACE_TRACEABILITY_NA``: path to a newline-delimited N/A id file.
- ``DEXPACE_TRACEABILITY_GATE``: ``1``/``true``/``yes`` to fail on gaps.

Run as a script to dump the placeholder matrix (no coverage data outside a
pytest run):

    python tools/traceability_audit.py --format json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest import Config, Item, Session

__all__ = [
    "MARKER_NAME",
    "PLACEHOLDER_INDEX",
    "AuditResult",
    "Gap",
    "Level",
    "Requirement",
    "RequirementIndex",
    "TraceabilityPlugin",
    "audit",
    "build_index",
    "build_plugin_from_env",
    "default_index",
    "default_index_path",
    "default_matrix_path",
    "load_index_file",
    "na_ids_from_file",
    "render_matrix_csv",
    "render_matrix_json",
    "repo_root",
]

#: The custom pytest marker name. Registered centrally in the root
#: ``pyproject.toml`` ``[tool.pytest.ini_options].markers`` list so it collects
#: warning-free under ``-W error::pytest.PytestUnknownMarkWarning``.
MARKER_NAME = "req"

#: Canonical schema tag written into the JSON matrix artifact, bumped only on a
#: breaking change to the artifact shape.
_SCHEMA = "dexpace-traceability-matrix/v1"

#: Environment variable names consumed by ``build_plugin_from_env``.
ENV_MATRIX = "DEXPACE_TRACEABILITY_MATRIX"
ENV_ENABLE = "DEXPACE_TRACEABILITY_ENABLE"
ENV_NA = "DEXPACE_TRACEABILITY_NA"
ENV_GATE = "DEXPACE_TRACEABILITY_GATE"


class Level(Enum):
    """Normative strength of a requirement, per RFC 2119 keywords.

    Only ``MUST`` requirements gate the audit; ``SHOULD`` / ``MAY`` are recorded
    in the matrix for visibility but never counted as a coverage gap.
    """

    MUST = "MUST"
    SHOULD = "SHOULD"
    MAY = "MAY"


@dataclass(frozen=True, slots=True)
class Requirement:
    """A single normative requirement drawn from the product spec.

    Attributes:
        id: The stable requirement identifier (e.g. ``RETRY-19``). This is the
            value a test passes to ``@pytest.mark.req(...)``.
        level: The RFC 2119 strength of the requirement.
        subsystem: The area the requirement belongs to (e.g. ``retry``). Used
            for milestone-scoped gating.
        description: A one-line human summary of the requirement.
    """

    id: str
    level: Level
    subsystem: str
    description: str


@dataclass(frozen=True, slots=True)
class RequirementIndex:
    """An immutable collection of requirements keyed by id.

    Build via ``build_index`` so duplicate ids are rejected up front.
    """

    requirements: tuple[Requirement, ...]

    def by_id(self) -> dict[str, Requirement]:
        """Return a mapping of requirement id -> requirement."""
        return {req.id: req for req in self.requirements}

    def ids(self) -> frozenset[str]:
        """Return the set of all requirement ids in the index."""
        return frozenset(req.id for req in self.requirements)

    def subsystems(self) -> frozenset[str]:
        """Return the set of distinct subsystem names in the index."""
        return frozenset(req.subsystem for req in self.requirements)


@dataclass(frozen=True, slots=True)
class Gap:
    """A MUST-level requirement that has no covering test marker.

    Attributes:
        requirement: The uncovered requirement.
        reason: A short human explanation (always "no covering test marker"
            today; kept as a field so future audit rules can distinguish
            gap kinds).
    """

    requirement: Requirement
    reason: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The outcome of one audit pass.

    Attributes:
        matrix: Every indexed requirement id -> the sorted tuple of test node
            ids that claim it (empty tuple when uncovered).
        gaps: The MUST-level gaps in the currently enabled subsystems, sorted
            by requirement id.
        unknown_marker_ids: Sorted ids referenced by a marker but absent from
            the index (likely a typo or a not-yet-indexed requirement).
        enabled_subsystems: The subsystems that were gated this pass.
        na_ids: The requirement ids treated as not-applicable (never gaps).
    """

    matrix: Mapping[str, tuple[str, ...]]
    gaps: tuple[Gap, ...]
    unknown_marker_ids: tuple[str, ...]
    enabled_subsystems: frozenset[str]
    na_ids: frozenset[str]

    @property
    def ok(self) -> bool:
        """Return whether the audit found no gaps."""
        return not self.gaps


def build_index(requirements: Iterable[Requirement]) -> RequirementIndex:
    """Build a ``RequirementIndex``, rejecting duplicate ids.

    Args:
        requirements: The requirements to index.

    Returns:
        The immutable index.

    Raises:
        ValueError: If two requirements share an id.
    """
    reqs = tuple(requirements)
    seen: set[str] = set()
    for req in reqs:
        if req.id in seen:
            raise ValueError(f"duplicate requirement id: {req.id}")
        seen.add(req.id)
    return RequirementIndex(reqs)


def _build_full_matrix(
    index: RequirementIndex, collected: Mapping[str, Sequence[str]]
) -> dict[str, tuple[str, ...]]:
    """Map every indexed requirement id to its sorted, de-duplicated test ids.

    Requirements with no collected marker map to an empty tuple, so the matrix
    is a complete view of the index rather than only the covered subset.
    """
    return {
        req.id: tuple(sorted(set(collected.get(req.id, ()))))
        for req in sorted(index.requirements, key=lambda r: r.id)
    }


def audit(
    index: RequirementIndex,
    collected: Mapping[str, Sequence[str]],
    *,
    enabled_subsystems: Iterable[str] = (),
    na_ids: Iterable[str] = (),
) -> AuditResult:
    """Cross-reference collected markers against the requirement index.

    Args:
        index: The requirement index to audit against.
        collected: Requirement id -> the test node ids that marked it.
        enabled_subsystems: Subsystems that are "done" and therefore gated; a
            MUST requirement outside these is never a gap. Empty means gate
            nothing (the default milestone posture).
        na_ids: Requirement ids intentionally not implemented; never gaps.

    Returns:
        The ``AuditResult`` for this pass.
    """
    enabled = frozenset(enabled_subsystems)
    not_applicable = frozenset(na_ids)
    matrix = _build_full_matrix(index, collected)
    by_id = index.by_id()
    gaps = tuple(
        Gap(req, "no covering test marker")
        for req in sorted(index.requirements, key=lambda r: r.id)
        if req.level is Level.MUST
        and req.subsystem in enabled
        and req.id not in not_applicable
        and not matrix[req.id]
    )
    unknown = tuple(sorted(rid for rid in collected if rid not in by_id))
    return AuditResult(
        matrix=matrix,
        gaps=gaps,
        unknown_marker_ids=unknown,
        enabled_subsystems=enabled,
        na_ids=not_applicable,
    )


def _summary(result: AuditResult, index: RequirementIndex) -> dict[str, object]:
    """Build the summary block embedded in the JSON matrix artifact."""
    reqs = index.requirements
    must = [req for req in reqs if req.level is Level.MUST]
    covered = sum(1 for tests in result.matrix.values() if tests)
    must_covered = sum(1 for req in must if result.matrix[req.id])
    return {
        "total_requirements": len(reqs),
        "covered": covered,
        "uncovered": len(reqs) - covered,
        "must_total": len(must),
        "must_covered": must_covered,
        "gap_count": len(result.gaps),
        "gaps": [gap.requirement.id for gap in result.gaps],
        "enabled_subsystems": sorted(result.enabled_subsystems),
        "na_ids": sorted(result.na_ids),
        "unknown_marker_ids": list(result.unknown_marker_ids),
        # Clearly-labeled "pending" bucket: requirement ids that tests claim via
        # a marker but that the current index does not define yet — behaviours
        # exercised and traceable, awaiting an id in the real spec index (e.g.
        # transport, observability, jsonl/set-date/client-identity policies).
        # A positively-named alias of ``unknown_marker_ids`` so the artifact
        # surfaces these without implying a typo, and never as a gap/failure.
        "pending_ids": list(result.unknown_marker_ids),
    }


def render_matrix_json(result: AuditResult, index: RequirementIndex) -> str:
    """Render the audit result as canonical, newline-terminated JSON text.

    Args:
        result: The audit result to render.
        index: The index the result was produced against (for requirement
            metadata).

    Returns:
        A deterministic JSON document (sorted keys, requirements sorted by id)
        suitable for committing or diffing.
    """
    by_id = index.by_id()
    rows: list[dict[str, object]] = []
    for rid in sorted(result.matrix):
        req = by_id[rid]
        tests = list(result.matrix[rid])
        rows.append(
            {
                "id": rid,
                "level": req.level.value,
                "subsystem": req.subsystem,
                "description": req.description,
                "covered": bool(tests),
                "tests": tests,
            }
        )
    payload = {"schema": _SCHEMA, "summary": _summary(result, index), "requirements": rows}
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_matrix_csv(result: AuditResult, index: RequirementIndex) -> str:
    """Render the audit result as CSV text, one row per requirement.

    Args:
        result: The audit result to render.
        index: The index the result was produced against.

    Returns:
        CSV text with a header row followed by one row per requirement id.
    """
    by_id = index.by_id()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["requirement_id", "level", "subsystem", "covered", "test_count", "tests", "description"]
    )
    for rid in sorted(result.matrix):
        req = by_id[rid]
        tests = result.matrix[rid]
        writer.writerow(
            [
                rid,
                req.level.value,
                req.subsystem,
                str(bool(tests)),
                str(len(tests)),
                ";".join(tests),
                req.description,
            ]
        )
    return buffer.getvalue()


def na_ids_from_file(path: Path | str | None) -> frozenset[str]:
    """Read intentionally-not-applicable requirement ids from a ledger file.

    This is the seam the deviations ledger plugs into. The accepted format is
    one requirement id per line, optionally followed by an inline ``#`` comment
    giving the justification (``RETRY-9  # deviation: ...``); blank lines and
    whole-line ``#`` comments are ignored. A ``None`` path or a missing file
    yields an empty set, so the audit works before the ledger exists.

    Args:
        path: Path to the N/A id file, or ``None``.

    Returns:
        The frozenset of not-applicable requirement ids.
    """
    if path is None:
        return frozenset()
    resolved = Path(path)
    if not resolved.is_file():
        return frozenset()
    ids: set[str] = set()
    for line in resolved.read_text(encoding="utf-8").splitlines():
        # Drop any inline comment, then take the bare id token.
        token = line.split("#", 1)[0].strip()
        if token:
            ids.add(token)
    return frozenset(ids)


def repo_root() -> Path:
    """Return the workspace root (this file sits at ``tools/`` under it)."""
    return Path(__file__).resolve().parent.parent


def default_matrix_path() -> Path:
    """Return the default matrix artifact path (under gitignored ``build/``)."""
    return repo_root() / "build" / "traceability-matrix.json"


def default_index_path() -> Path:
    """Return the vendored requirement-index path (``tools/requirement_index.json``)."""
    return repo_root() / "tools" / "requirement_index.json"


def load_index_file(path: Path | str) -> RequirementIndex:
    """Load a requirement index from a vendored JSON projection of Appendix C.

    The file is produced by ``tools/build_requirement_index.py`` and carries a
    ``requirements`` array of ``{id, level, subsystem, description}`` objects
    (levels already normalized to the ``Level`` vocabulary).

    Args:
        path: Path to the vendored index JSON.

    Returns:
        The immutable requirement index.

    Raises:
        ValueError: If the document has an unexpected schema or a duplicate id.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = document.get("schema", "")
    if not schema.startswith("dexpace-requirement-index/"):
        raise ValueError(f"not a requirement index document: schema={schema!r}")
    return build_index(
        Requirement(
            id=row["id"],
            level=Level(row["level"]),
            subsystem=row["subsystem"],
            description=row["description"],
        )
        for row in document["requirements"]
    )


def default_index() -> RequirementIndex:
    """Return the real vendored index, or the placeholder if it is absent.

    CI and local runs use the committed ``requirement_index.json``; the
    placeholder fallback keeps the audit importable in a stripped checkout that
    lacks the vendored file (the machinery unit tests exercise the placeholder
    directly and do not depend on this fallback).
    """
    path = default_index_path()
    return load_index_file(path) if path.is_file() else PLACEHOLDER_INDEX


# --- Placeholder requirement index -----------------------------------------
#
# NOT the real spec. A handful of representative requirements whose subsystems
# and behaviours are real (drawn from the retry / redirect / auth / bodies /
# http / pagination / sse / webhooks / tracing areas that already exist in the
# codebase), used purely to prove the audit machinery end-to-end. Replace this
# whole constant with the real index once it exists — nothing else changes.
PLACEHOLDER_INDEX = build_index(
    [
        Requirement("RETRY-19", Level.MUST, "retry", "Honor a server-supplied Retry-After header."),
        Requirement(
            "RETRY-07", Level.MUST, "retry", "Do not retry a non-idempotent method by default."
        ),
        Requirement("RETRY-31", Level.SHOULD, "retry", "Apply jitter to computed backoff delays."),
        Requirement(
            "REDIRECT-03", Level.MUST, "redirect", "Follow 301/302/303/307/308 up to the maximum."
        ),
        Requirement(
            "REDIRECT-11",
            Level.MUST,
            "redirect",
            "Strip the Authorization header on a cross-origin redirect.",
        ),
        Requirement(
            "IDEMPOTENCY-05",
            Level.MUST,
            "idempotency",
            "Attach an idempotency key to retried unsafe requests.",
        ),
        Requirement(
            "AUTH-02", Level.MUST, "auth", "Send the bearer token in the Authorization header."
        ),
        Requirement(
            "AUTH-14", Level.SHOULD, "auth", "Cache tokens until the refresh-ahead window."
        ),
        Requirement(
            "BODY-08", Level.MUST, "bodies", "Raise on second consumption of a single-use body."
        ),
        Requirement(
            "BODY-21", Level.MAY, "bodies", "Expose a replayable body via to_replayable()."
        ),
        Requirement("HEADER-04", Level.MUST, "http", "Compare header names case-insensitively."),
        Requirement(
            "PAGINATION-06", Level.SHOULD, "pagination", "Follow RFC 8288 Link rel=next headers."
        ),
        Requirement("SSE-09", Level.MUST, "sse", "Join multi-line data fields with newlines."),
        Requirement(
            "WEBHOOK-12", Level.MUST, "webhooks", "Reject signatures failing constant-time compare."
        ),
        Requirement(
            "TRACING-01", Level.SHOULD, "tracing", "Propagate a correlation id across stages."
        ),
    ]
)


class TraceabilityPlugin:
    """A pytest plugin that collects ``req`` markers and emits the matrix.

    Registered from the root ``conftest.py`` via ``build_plugin_from_env``. On
    every run it writes the matrix artifact; it only *fails* the run when
    ``gate`` is set and an enabled subsystem has an uncovered MUST requirement.
    """

    def __init__(
        self,
        index: RequirementIndex | None = None,
        *,
        output_path: Path | str | None = None,
        enabled_subsystems: Iterable[str] = (),
        na_ids: Iterable[str] = (),
        gate: bool = False,
    ) -> None:
        """Configure the plugin.

        Args:
            index: The requirement index (defaults to ``PLACEHOLDER_INDEX``).
            output_path: Where to write the JSON matrix (defaults to
                ``default_matrix_path()``).
            enabled_subsystems: Subsystems to gate on.
            na_ids: Requirement ids to treat as not-applicable.
            gate: Whether to fail the run on a gap.
        """
        self._index = index if index is not None else PLACEHOLDER_INDEX
        self._output_path = Path(output_path) if output_path else default_matrix_path()
        self._enabled = frozenset(enabled_subsystems)
        self._na = frozenset(na_ids)
        self._gate = gate
        self._collected: dict[str, list[str]] = {}

    def pytest_collection_modifyitems(self, config: Config, items: list[Item]) -> None:
        """Record every ``@pytest.mark.req(id)`` claim against its test node."""
        for item in items:
            for mark in item.iter_markers(name=MARKER_NAME):
                for rid in mark.args:
                    self._collected.setdefault(str(rid), []).append(item.nodeid)

    def pytest_collection_finish(self, session: Session) -> None:
        """Write the matrix artifact and, when gated, fail on any MUST gap."""
        import pytest

        result = audit(
            self._index,
            self._collected,
            enabled_subsystems=self._enabled,
            na_ids=self._na,
        )
        self._write(result)
        if self._gate and result.gaps:
            ids = ", ".join(gap.requirement.id for gap in result.gaps)
            raise pytest.UsageError(
                f"traceability audit failed: uncovered MUST requirement(s): {ids}"
            )

    def _write(self, result: AuditResult) -> None:
        """Serialise the matrix to the configured output path."""
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(render_matrix_json(result, self._index), encoding="utf-8")


def _split_env(value: str | None) -> frozenset[str]:
    """Split a comma-separated env value into a set of non-empty tokens."""
    if not value:
        return frozenset()
    return frozenset(token.strip() for token in value.split(",") if token.strip())


def build_plugin_from_env() -> TraceabilityPlugin:
    """Build a ``TraceabilityPlugin`` configured from environment variables.

    Returns:
        A plugin honouring ``DEXPACE_TRACEABILITY_*`` overrides; with none set
        it emits the matrix to the default path and gates nothing.
    """
    na_path = os.environ.get(ENV_NA)
    gate = os.environ.get(ENV_GATE, "").strip().lower() in {"1", "true", "yes"}
    return TraceabilityPlugin(
        default_index(),
        output_path=os.environ.get(ENV_MATRIX),
        enabled_subsystems=_split_env(os.environ.get(ENV_ENABLE)),
        na_ids=na_ids_from_file(na_path) if na_path else (),
        gate=gate,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: dump the placeholder matrix (no coverage data).

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        A process exit code (always ``0`` on success).
    """
    parser = argparse.ArgumentParser(
        description="Render the placeholder traceability matrix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument(
        "--write", action="store_true", help="write to the default matrix path instead of stdout"
    )
    args = parser.parse_args(argv)
    # Dump the real vendored index (no coverage data outside a pytest run).
    index = default_index()
    result = audit(index, {})
    if args.format == "csv":
        text = render_matrix_csv(result, index)
    else:
        text = render_matrix_json(result, index)
    if args.write:
        path = default_matrix_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote matrix to {path}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
