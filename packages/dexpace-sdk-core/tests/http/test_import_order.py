# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Regression test: importing the request package first must not deadlock.

The request package re-exports ``AsyncRequestBody``, which historically pulled
in the async response body, which in turn did a runtime import of
``SupportsAsyncRead`` back from the still-initializing request module — a cycle
that broke ``from dexpace.sdk.core.http.request import Request`` as a *first*
import. The fix scopes that back-reference to ``TYPE_CHECKING`` so the request
package imports cleanly regardless of order. This test re-checks the property
in a fresh interpreter so import caching from sibling tests cannot mask a
regression.
"""

from __future__ import annotations

import subprocess
import sys


def test_request_package_imports_first_in_fresh_interpreter() -> None:
    code = "from dexpace.sdk.core.http.request import Request; print(Request.__name__)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Request"
