# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Guards for the *consumer-facing* API contract that the surface baseline alone
does not pin down.

Two mechanisms live here:

- **Protocol / ABC member pins.** Every seam a downstream consumer implements
  (`HttpClient`, `Serde`, `PaginationStrategy`, `Policy`, …) has its required
  member set frozen. Adding a member to one of these is a breaking change for
  every existing implementer, so a new capability must arrive as a *new*
  optional Protocol, not a widened existing one. These tests fail the moment a
  member is silently added or removed, forcing that decision to be explicit.
- **A seeded-change proof.** A faithful copy of the source trees reproduces the
  committed surface baseline exactly; renaming a single public method in the
  copy then makes `build_surface` diverge. This proves the snapshot-diff gate
  in ``test_public_surface.py`` actually catches a public-signature change
  rather than passing vacuously.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import typing
from pathlib import Path
from types import ModuleType

import pytest

from dexpace.sdk.core.client import AsyncHttpClient, HttpClient
from dexpace.sdk.core.pagination import HasHeaders, PaginationStrategy
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.serde import Deserializer, Serde, Serializer

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "surface_snapshot.py"
_BASELINE_PATH = _REPO_ROOT / "tools" / "surface_baseline.json"

# Distribution dir -> namespace sub-path, mirrored from surface_snapshot so the
# seeded-change proof can copy each ``src`` tree faithfully.
_DISTRIBUTIONS = {
    "dexpace-sdk-core": "dexpace/sdk/core",
    "dexpace-sdk-http-stdlib": "dexpace/sdk/http/stdlib",
    "dexpace-sdk-http-httpx": "dexpace/sdk/http/httpx",
    "dexpace-sdk-http-aiohttp": "dexpace/sdk/http/aiohttp",
    "dexpace-sdk-http-requests": "dexpace/sdk/http/requests",
}

# --- Pinned required-member sets -------------------------------------------
#
# Each entry freezes the members a downstream consumer must supply to satisfy
# the Protocol. Adding one here without a deliberate review is exactly the
# silent-widening this guard exists to reject.
_PROTOCOL_MEMBERS: dict[type, frozenset[str]] = {
    HttpClient: frozenset({"execute"}),
    AsyncHttpClient: frozenset({"execute"}),
    # media_type added deliberately (PY-M5-04): each bundle declares its own
    # wire format rather than the SPI defaulting one at the call site.
    Serde: frozenset({"serializer", "deserializer", "media_type"}),
    Serializer: frozenset({"serialize", "serialize_to_bytes", "serialize_to_stream"}),
    Deserializer: frozenset({"deserialize", "deserialize_bytes", "deserialize_stream"}),
    PaginationStrategy: frozenset({"parse"}),
    HasHeaders: frozenset({"headers"}),
}


def _load_snapshot_tool() -> ModuleType:
    """Import ``tools/surface_snapshot.py`` by file path (``tools`` is not a package)."""
    spec = importlib.util.spec_from_file_location("_surface_snapshot_pins", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load surface snapshot tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _protocol_members(proto: type) -> frozenset[str]:
    """Return the required member names of a `typing.Protocol`.

    Computed the same way `typing.get_protocol_members` does — walking the MRO
    for annotated attributes and body-defined methods / properties, skipping the
    ``typing`` machinery (`Protocol`, `Generic`) and ``object`` — so the pin is
    reproducible on Python 3.12, which predates that public helper.

    Args:
        proto: A Protocol class (subscripted generics are fine; the origin's
            MRO is walked).

    Returns:
        The frozen set of public member names the Protocol requires.
    """
    members: set[str] = set()
    for klass in proto.__mro__:
        if klass is object or klass.__module__ == "typing":
            continue
        members.update(
            name for name in getattr(klass, "__annotations__", {}) if not name.startswith("_")
        )
        for name, value in vars(klass).items():
            if not name.startswith("_") and (callable(value) or isinstance(value, property)):
                members.add(name)
    return frozenset(members)


@pytest.mark.req("NFR-3")
@pytest.mark.parametrize(
    ("proto", "expected"),
    list(_PROTOCOL_MEMBERS.items()),
    ids=lambda arg: arg.__name__ if isinstance(arg, type) else "",
)
def test_protocol_required_members_are_pinned(proto: type, expected: frozenset[str]) -> None:
    assert _protocol_members(proto) == expected, (
        f"{proto.__name__}'s required member set changed. Adding a required member "
        "is a breaking change for every existing implementer — ship the new "
        "capability as a NEW optional Protocol instead of widening this one. If "
        "the change is truly intended, update the pin in _PROTOCOL_MEMBERS."
    )


def test_protocol_member_helper_matches_stdlib_when_available() -> None:
    # On 3.13+ the stdlib grew ``typing.get_protocol_members``; cross-check the
    # hand-rolled walker against it so the 3.12-compatible fallback cannot drift.
    stdlib_impl = getattr(typing, "get_protocol_members", None)
    if stdlib_impl is None:
        pytest.skip("typing.get_protocol_members is unavailable before Python 3.13")
    for proto in _PROTOCOL_MEMBERS:
        assert _protocol_members(proto) == frozenset(stdlib_impl(proto))


@pytest.mark.parametrize(
    ("policy", "abstract"),
    [(Policy, frozenset({"send"})), (AsyncPolicy, frozenset({"send"}))],
    ids=["Policy", "AsyncPolicy"],
)
def test_policy_abstract_member_set_is_pinned(
    policy: type[Policy] | type[AsyncPolicy], abstract: frozenset[str]
) -> None:
    # ``Policy`` / ``AsyncPolicy`` are consumer-subclassed ABCs: a new abstract
    # method would break every existing subclass, so the abstract set is pinned.
    assert policy.__abstractmethods__ == abstract
    # The declared class-level requirements (wired by the pipeline, not the
    # subclass) are likewise frozen so neither silently grows.
    assert set(policy.__dict__.get("__annotations__", {})) == {"STAGE", "next"}


def _copy_source_trees(destination: Path) -> None:
    """Copy every distribution's ``src`` tree under ``destination/packages``."""
    for dist, namespace in _DISTRIBUTIONS.items():
        src = _REPO_ROOT / "packages" / dist / "src"
        if not (src / namespace).is_dir():
            continue
        shutil.copytree(src, destination / "packages" / dist / "src")


@pytest.mark.req("NFR-4")
def test_seeded_public_signature_change_breaks_the_surface_diff(tmp_path: Path) -> None:
    tool = _load_snapshot_tool()
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    _copy_source_trees(tmp_path)

    # A faithful copy must reproduce the committed baseline exactly; otherwise
    # the proof below would pass for the wrong reason.
    assert tool.build_surface(tmp_path) == baseline

    # Seed a public-signature change: rename the ``HttpClient.execute`` seam.
    seam = (
        tmp_path
        / "packages"
        / "dexpace-sdk-core"
        / "src"
        / ("dexpace/sdk/core/client/http_client.py")
    )
    source = seam.read_text(encoding="utf-8")
    assert "def execute(" in source
    seam.write_text(source.replace("def execute(", "def execute_renamed("), encoding="utf-8")

    mutated = tool.build_surface(tmp_path)
    assert mutated != baseline, "the snapshot diff failed to detect a renamed public method"

    client_defs = mutated["dexpace-sdk-core"]["definitions"]["dexpace.sdk.core.client.http_client"]
    assert "execute" not in client_defs["HttpClient"]["methods"]
    assert "execute_renamed" in client_defs["HttpClient"]["methods"]
