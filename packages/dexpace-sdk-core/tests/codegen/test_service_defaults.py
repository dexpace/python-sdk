# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for `ServiceDefaults` and its field-wise merge helper.

Pure, no pipeline construction involved — the integration-level "exactly one
pipeline" / UA-ordering / api-version / close-matrix scenarios live in
`test_merge_config.py`.
"""

from __future__ import annotations

from dexpace.sdk.core.codegen.service_defaults import ServiceDefaults, merge_service_defaults
from dexpace.sdk.core.codegen.status_error_map import StatusErrorMap


def test_bare_service_defaults_has_every_field_none() -> None:
    defaults = ServiceDefaults()
    assert defaults.base_url is None
    assert defaults.user_agent_token is None
    assert defaults.api_version is None
    assert defaults.api_version_header is None
    assert defaults.errors is None
    assert defaults.serde is None


def test_merge_with_no_user_override_returns_defaults_unchanged() -> None:
    defaults = ServiceDefaults(base_url="https://svc.example.com", user_agent_token="svc/1.0")
    assert merge_service_defaults(defaults, None) is defaults


def test_merge_is_field_wise_not_object_wise() -> None:
    # The user overrides only base_url; every other field must still resolve
    # from the service defaults rather than falling all the way through to
    # "unset" just because *some* field on the override was set.
    defaults = ServiceDefaults(
        base_url="https://svc.example.com",
        user_agent_token="svc/1.0",
        api_version="2026-01-01",
    )
    user = ServiceDefaults(base_url="https://override.example.com")
    merged = merge_service_defaults(defaults, user)
    assert merged.base_url == "https://override.example.com"
    assert merged.user_agent_token == "svc/1.0"
    assert merged.api_version == "2026-01-01"


def test_merge_user_wins_on_every_field_they_set() -> None:
    service_errors = StatusErrorMap()
    user_errors = StatusErrorMap()
    defaults = ServiceDefaults(
        base_url="https://svc.example.com",
        user_agent_token="svc/1.0",
        api_version="2026-01-01",
        api_version_header="Svc-Api-Version",
        errors=service_errors,
    )
    user = ServiceDefaults(
        base_url="https://override.example.com",
        user_agent_token="caller/9.9",
        api_version="2099-12-31",
        api_version_header="X-Api-Version",
        errors=user_errors,
    )
    merged = merge_service_defaults(defaults, user)
    assert merged.base_url == "https://override.example.com"
    assert merged.user_agent_token == "caller/9.9"
    assert merged.api_version == "2099-12-31"
    assert merged.api_version_header == "X-Api-Version"
    assert merged.errors is user_errors
