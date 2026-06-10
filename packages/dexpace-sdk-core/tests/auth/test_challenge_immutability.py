# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the read-only parameter view on parsed challenges.

``AuthenticateChallenge`` is a frozen dataclass; its ``parameters`` mapping
produced by ``parse_challenges`` must likewise be immutable so the object is
fully read-only, not a frozen shell wrapping a mutable dict.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from dexpace.sdk.core.http.auth import parse_challenges


class TestParameterImmutability:
    def test_parameters_is_a_mapping(self) -> None:
        challenge = parse_challenges('Digest realm="r", qop="auth"')[0]
        assert isinstance(challenge.parameters, Mapping)
        assert challenge.parameters["realm"] == "r"
        assert challenge.parameters["qop"] == "auth"

    def test_parameters_view_rejects_mutation(self) -> None:
        challenge = parse_challenges('Digest realm="r"')[0]
        assert isinstance(challenge.parameters, MappingProxyType)
        with pytest.raises(TypeError):
            challenge.parameters["realm"] = "tampered"  # type: ignore[index]

    def test_multiple_challenges_have_independent_params(self) -> None:
        first, second = parse_challenges('Basic realm="a", Digest realm="b", qop="auth"')
        assert first.parameters == {"realm": "a"}
        assert second.parameters == {"realm": "b", "qop": "auth"}
