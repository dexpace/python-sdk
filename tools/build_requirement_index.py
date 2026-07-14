# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Generate the vendored requirement index from the product spec's Appendix C.

The normative spec — ``java-sdk/docs/product-spec.md`` — lives outside this
repository, but the traceability audit (``tools/traceability_audit.py``) needs
the full requirement index available in-repo and in CI, where the spec is not
checked out. This script parses Appendix C ("Consolidated Normative Requirement
Index") into ``tools/requirement_index.json``, a committed, machine-readable
projection that the audit loads as its real index.

Regenerate the vendored file whenever the spec's Appendix C changes:

    python tools/build_requirement_index.py path/to/product-spec.md

The output is deterministic (rows in Appendix C order, sorted keys), so a
regeneration with an unchanged spec is a no-op diff. Each requirement carries a
stable subsystem *slug* (see ``_SUBSYSTEM_SLUGS``) rather than the spec's long
prose subsystem name, so the audit's milestone-scoped enablement can name
subsystems in an environment variable without quoting spaces and parentheses.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: Map each Appendix C subsystem prose name to a short, stable slug. The audit
#: gates on these slugs (``DEXPACE_TRACEABILITY_ENABLE=http,retry,...``). Keyed
#: by the exact subsystem string as it appears in the Appendix C table.
_SUBSYSTEM_SLUGS: dict[str, str] = {
    "Product vision, pluggable seams and extension model": "seams",
    "Core HTTP domain model": "http",
    "Request/response body and body-logging lifecycle": "bodies",
    "I/O streaming contracts": "io",
    "Execution context model (context promotion chain + ContextStore backstop)": "context",
    "Stage-based execution pipeline runtime (http.pipeline)": "pipeline",
    "Recovery-chain pipeline primitives": "recovery",
    "Redirect handling (HTTP 3xx following in the synchronous stage-based pipeline)": "redirect",
    "Retry and resilience": "retry",
    "Authentication": "auth",
    "Cross-cutting invariants and policies": "xcut",
    "Serialization (Serde)": "serde",
    "Pagination": "pagination",
    "Server-Sent Events and streaming": "sse",
    "Instrumentation and observability": "observability",
    "Configuration and utilities": "config",
    "Transport adapter conformance contract": "transport",
    "Asynchronous runtime adapter contract": "async",
    "Non-functional requirements and quality bar": "nfr",
}

#: Row shape: leading ``|`` then an uppercase requirement id in the first cell.
_ROW = re.compile(r"^\|\s*([A-Z][A-Z0-9-]*)\s*\|")

#: Canonical schema tag for the vendored artifact.
_SCHEMA = "dexpace-requirement-index/v1"

_LEVELS = {"MUST": "MUST", "MUSTNOT": "MUST", "SHOULD": "SHOULD", "MAY": "MAY"}


def _normalize_level(raw: str) -> str:
    """Map a spec level token to the audit's ``Level`` vocabulary.

    ``MUST NOT`` is a prohibition — normatively MUST-strength — so it folds to
    ``MUST`` for gating. ``SHOULD``/``MAY`` pass through.
    """
    key = raw.upper().replace(" ", "")
    try:
        return _LEVELS[key]
    except KeyError:
        raise SystemExit(f"unknown requirement level: {raw!r}") from None


def parse_appendix_c(spec_text: str) -> list[dict[str, str]]:
    """Parse the Appendix C requirement table into a list of requirement dicts.

    Args:
        spec_text: The full product-spec markdown.

    Returns:
        Requirements in Appendix C order, each with ``id``, ``level``,
        ``subsystem`` (slug), and ``description``.

    Raises:
        SystemExit: If the appendix is missing, a row is malformed, or a
            subsystem name has no slug mapping.
    """
    lines = spec_text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## Appendix C"))
    except StopIteration:
        raise SystemExit("Appendix C heading not found in spec") from None

    requirements: list[dict[str, str]] = []
    for line in lines[start:]:
        if not _ROW.match(line):
            continue
        # Only a description cell can contain a table pipe, escaped as ``\|``;
        # protect it before splitting so real cell separators stay unambiguous.
        cells = [c.strip() for c in line.replace(r"\|", "\x00").strip().strip("|").split("|")]
        if len(cells) < 4:
            raise SystemExit(f"malformed Appendix C row: {line[:80]!r}")
        rid, level, subsystem = cells[0], cells[1], cells[2]
        if rid == "ID" and level == "Level":
            continue  # header row
        slug = _SUBSYSTEM_SLUGS.get(subsystem)
        if slug is None:
            raise SystemExit(f"no slug for subsystem: {subsystem!r}")
        requirements.append(
            {
                "id": rid,
                "level": _normalize_level(level),
                "subsystem": slug,
                "description": "|".join(cells[3:]).replace("\x00", "|"),
            }
        )
    return requirements


def render(requirements: list[dict[str, str]], *, source: str) -> str:
    """Render the vendored index JSON document (deterministic, newline-terminated)."""
    ids = [r["id"] for r in requirements]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"duplicate requirement ids in Appendix C: {duplicates}")
    payload = {
        "schema": _SCHEMA,
        "source": source,
        "count": len(requirements),
        "requirements": requirements,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def default_output_path() -> Path:
    """Return the committed vendored-index path (``tools/requirement_index.json``)."""
    return Path(__file__).resolve().parent / "requirement_index.json"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse a spec file and write the vendored index."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spec", type=Path, help="path to product-spec.md")
    parser.add_argument("-o", "--output", type=Path, default=default_output_path())
    args = parser.parse_args(argv)
    text = args.spec.read_text(encoding="utf-8")
    requirements = parse_appendix_c(text)
    # Provenance records the appendix, not the machine-specific spec path.
    document = render(requirements, source="java-sdk/docs/product-spec.md#appendix-c")
    args.output.write_text(document, encoding="utf-8")
    sys.stdout.write(f"wrote {len(requirements)} requirements to {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
