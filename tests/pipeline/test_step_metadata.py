"""Tests for ``StepMetadata`` construction semantics."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.pipeline import StepMetadata


def test_step_metadata_requires_name() -> None:
    """``name`` is mandatory — there is no meaningful default."""
    with pytest.raises(TypeError):
        StepMetadata()  # type: ignore[call-arg]


def test_step_metadata_defaults_are_empty_strings() -> None:
    """``description`` and ``version`` default to ``""``, not placeholders."""
    meta = StepMetadata(name="retry")
    assert meta.name == "retry"
    assert meta.description == ""
    assert meta.version == ""
    assert meta.tags == ()


def test_step_metadata_accepts_all_fields() -> None:
    """All four fields can be supplied positionally or by keyword."""
    meta = StepMetadata(
        name="retry",
        description="exponential backoff",
        version="1.0.0",
        tags=("transient", "io"),
    )
    assert meta.name == "retry"
    assert meta.description == "exponential backoff"
    assert meta.version == "1.0.0"
    assert meta.tags == ("transient", "io")
