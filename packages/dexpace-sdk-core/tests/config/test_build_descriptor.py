# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``BuildDescriptor`` — distribution build identity with fallback."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest

from dexpace.sdk.core.config import BuildDescriptor
from dexpace.sdk.core.config import build_descriptor as _module


@pytest.mark.req("CFG-36")
def test_resolve_reads_installed_version() -> None:
    # The core distribution is installed in the test venv, so a real version
    # resolves — and it is never blank.
    descriptor = BuildDescriptor.resolve()
    assert descriptor.name == "dexpace-sdk-core"
    assert descriptor.version
    assert descriptor.version != BuildDescriptor.UNKNOWN


@pytest.mark.req("CFG-36")
def test_resolve_falls_back_under_metadata_less_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a zipapp / frozen install where distribution metadata is absent."""

    def _raise(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(_module, "version", _raise)
    descriptor = BuildDescriptor.resolve()
    # The fallback engages and is a concrete, non-blank sentinel.
    assert descriptor.version == "unknown"
    assert descriptor.version.strip()
    # The distribution name is still reported (it needs no metadata).
    assert descriptor.name == "dexpace-sdk-core"


@pytest.mark.req("CFG-36")
def test_resolve_falls_back_on_blank_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_module, "version", lambda _name: "   ")
    descriptor = BuildDescriptor.resolve()
    assert descriptor.version == "unknown"


def test_resolve_blank_distribution_name_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_module, "version", lambda _name: "1.2.3")
    descriptor = BuildDescriptor.resolve("   ")
    assert descriptor.name == "unknown"
    assert descriptor.version == "1.2.3"


def test_descriptor_is_frozen() -> None:
    descriptor = BuildDescriptor(name="x", version="1")
    with pytest.raises((AttributeError, TypeError)):
        descriptor.version = "2"  # type: ignore[misc]
