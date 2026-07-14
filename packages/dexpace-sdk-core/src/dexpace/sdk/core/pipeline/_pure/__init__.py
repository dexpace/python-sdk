# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure algorithm layer for the pipeline.

Stdlib-only formula modules that carry no pipeline, transport, or error
dependencies, so both the sync and async policy twins (and error
construction) can import the single canonical definition instead of
restating it. Keeping these modules dependency-free is what lets the
import-linter layering contract place them below the pipeline runners.

Modules:
    classifier: retryability classification (status table + cause-chain walk).
    backoff: exponential-backoff delay computation and jitter application.
    pacing: server-supplied retry-pacing header parsing (``Retry-After`` /
        ``X-RateLimit-Reset``).
"""

from __future__ import annotations

__all__: list[str] = []
