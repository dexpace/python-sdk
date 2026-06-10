# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Guards that the dead ``RetryConfig`` surface stays removed.

``RetryConfig`` was unread public config that contradicted ``RetryPolicy``;
the real retry surface is ``RetryPolicy.__init__``. These tests pin the
removal so it cannot silently reappear.
"""

from __future__ import annotations

import importlib

import pytest

import dexpace.sdk.core.pipeline as pipeline
import dexpace.sdk.core.pipeline.step.config as config


def test_retry_config_not_exported_from_pipeline() -> None:
    assert "RetryConfig" not in pipeline.__all__
    assert not hasattr(pipeline, "RetryConfig")


def test_retry_config_not_exported_from_config_module() -> None:
    assert "RetryConfig" not in config.__all__
    assert not hasattr(config, "RetryConfig")


def test_step_metadata_still_available() -> None:
    assert "StepMetadata" in pipeline.__all__
    assert pipeline.StepMetadata is config.StepMetadata


def test_retry_config_cannot_be_imported() -> None:
    module = importlib.import_module("dexpace.sdk.core.pipeline.step.config")
    with pytest.raises(AttributeError):
        getattr(module, "RetryConfig")  # noqa: B009
