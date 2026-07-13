# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance battery for ``Status`` (HTTP-10, HTTP-11, HTTP-12).

`Status` lookup is total over integers (HTTP-10): a vendor ``499``, a
Cloudflare ``520``..``526``, or any other unregistered — even out-of-band —
code constructs without raising and carries the raw int, so a broken server
response is never discarded before a policy can react. Equality is by code
(HTTP-12) and the band helpers classify by hundreds range (HTTP-11).
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.response import Status


class TestUnregisteredCodesSynthesized:
    def test_unknown_in_range_code_is_synthesized(self) -> None:
        status = Status(218)
        assert isinstance(status, Status)
        assert int(status) == 218
        assert status == 218
        assert status.name == "UNKNOWN_218"
        assert status.value == 218

    def test_unknown_success_code_classifies_as_success(self) -> None:
        status = Status(218)
        assert status.is_success
        assert not status.is_redirect
        assert not status.is_error

    def test_unknown_server_error_code_classifies_as_server_error(self) -> None:
        status = Status(599)
        assert int(status) == 599
        assert status.name == "UNKNOWN_599"
        assert status.is_server_error
        assert status.is_error
        assert not status.is_success

    @pytest.mark.parametrize(
        "code",
        [499, 520, 521, 522, 523, 524, 525, 526],
        ids=["nginx-499", *[f"cf-{c}" for c in range(520, 527)]],
    )
    def test_vendor_codes_pass_through_construction(self, code: int) -> None:
        # HTTP-10: vendor codes outside the IANA registry must not be rejected.
        status = Status(code)
        assert int(status) == code
        assert status.name == f"UNKNOWN_{code}"

    def test_known_code_still_returns_named_member(self) -> None:
        assert Status(200) is Status.OK
        assert Status(404) is Status.NOT_FOUND


class TestTotalityOverInts:
    """HTTP-10: any integer constructs without raising; equality is by code."""

    def test_out_of_band_codes_construct_instead_of_raising(self) -> None:
        # These previously raised; a total lookup surfaces them with the raw int
        # so an odd status never costs the caller the live response.
        for code in (42, 99, 600, 700, 999, 1000, 0, -1):
            status = Status(code)
            assert int(status) == code
            assert status == code
            assert status.name == f"UNKNOWN_{code}"

    def test_totality_property_over_wide_range(self) -> None:
        codes = [*range(-5, 605), 700, 999, 1000, 100_000, -1_000]
        for code in codes:
            status = Status(code)  # must never raise
            assert isinstance(status, Status)
            assert int(status) == code
            assert status == code  # HTTP-12: equality by code

    def test_repeated_lookups_compare_equal(self) -> None:
        # Synthesized pseudo-members are not interned, but equality is by code
        # so repeated lookups of the same code compare (and hash) equal.
        assert Status(499) == Status(499)
        assert hash(Status(499)) == hash(Status(499))
        assert Status(499) != Status(500)

    def test_non_integer_lookup_raises(self) -> None:
        # A non-integer that does not coincide with a registered code value has
        # no member and is rejected (only ints are synthesized).
        for bad in ("200", 4.5, None, b"200"):
            with pytest.raises(ValueError):
                Status(bad)  # type: ignore[arg-type]


class TestEqualityByCode:
    """HTTP-12: a Status compares and hashes as its integer code."""

    def test_named_member_equals_int(self) -> None:
        # ``ok_code`` is typed ``int`` (not a literal) so the equality is checked
        # via the IntEnum's int-ness rather than rejected as a literal mismatch.
        ok_code: int = 200
        assert ok_code == Status.OK
        assert Status(200) == Status.OK

    def test_synthesized_member_equals_int(self) -> None:
        assert Status(499) == 499
        assert Status(499) != 500

    def test_usable_as_dict_key_and_set_member(self) -> None:
        seen = {Status.OK: "ok", Status(499): "vendor"}
        # A freshly synthesized member retrieves the entry stored under an equal
        # one — the hash/eq-by-code guarantee holds even without interning.
        assert seen[Status(200)] == "ok"
        assert seen[Status(499)] == "vendor"
        assert Status(200) in {Status.OK}


class TestBandClassification:
    """HTTP-11: is_informational/success/redirect/client_error/server_error/error."""

    @pytest.mark.parametrize(
        "code,predicate",
        [
            (100, "is_informational"),
            (199, "is_informational"),
            (200, "is_success"),
            (299, "is_success"),
            (300, "is_redirect"),
            (399, "is_redirect"),
            (400, "is_client_error"),
            (499, "is_client_error"),
            (500, "is_server_error"),
            (599, "is_server_error"),
        ],
    )
    def test_boundary_codes_land_in_expected_band(self, code: int, predicate: str) -> None:
        status = Status(code)
        assert getattr(status, predicate) is True

    @pytest.mark.parametrize("code", [400, 404, 499, 500, 503, 599])
    def test_error_covers_client_and_server(self, code: int) -> None:
        assert Status(code).is_error is True

    @pytest.mark.parametrize("code", [100, 200, 204, 301, 304])
    def test_error_false_for_non_error_bands(self, code: int) -> None:
        assert Status(code).is_error is False

    @pytest.mark.parametrize("code", [0, 42, 99, 600, 700, 999])
    def test_out_of_band_codes_match_no_band(self, code: int) -> None:
        status = Status(code)
        assert not status.is_informational
        assert not status.is_success
        assert not status.is_redirect
        assert not status.is_client_error
        assert not status.is_server_error
        assert not status.is_error
