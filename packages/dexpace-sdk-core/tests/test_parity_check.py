# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the sync/async parity gate (``tools/parity_check.py``).

The tool is not an installed package, so it is loaded by file path — the same
approach ``test_public_surface.py`` uses for its sibling tool. Coverage:

- a clean sync/async fixture pair normalizes to identical ASTs and passes;
- a fixture pair with a single seeded semantic drift fails in blocking mode and
  is reported (but does not fail) in report-only mode;
- the declared token map handles ``asyncio.sleep`` -> ``time.sleep``,
  ``async for`` -> ``for``, ``async with`` -> ``with``, and protocol method
  renames;
- report-only mode over the real hand-written policy twins produces a diff
  artifact without failing, even for the twins that legitimately diverge.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "parity_check.py"
_POLICIES = _REPO_ROOT / "packages/dexpace-sdk-core/src/dexpace/sdk/core/pipeline/policies"

# A clean, properly-paired sync/async fixture: the async twin differs only by
# async syntax and declared token swaps (``Policy``/``AsyncPolicy``,
# ``Response``/``AsyncResponse``, ``Clock``/``AsyncClock``, ...).
_CLEAN_SYNC = '''\
"""Sync reference module."""

from __future__ import annotations

from clock import SYSTEM_CLOCK, Clock
from policy import Policy


class SamplePolicy(Policy):
    """A sample sync policy."""

    def __init__(self, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock

    def send(self, request: object, ctx: object) -> Response:
        stamped = request.with_time(self._clock.now())
        for header in stamped.headers:
            stamped.mark(header)
        return self.next.send(stamped, ctx)
'''

_CLEAN_ASYNC = '''\
"""Async twin of the sample module."""

from __future__ import annotations

from clock import ASYNC_SYSTEM_CLOCK, AsyncClock
from async_policy import AsyncPolicy


class AsyncSamplePolicy(AsyncPolicy):
    """Async variant of the sample policy."""

    def __init__(self, *, clock: AsyncClock = ASYNC_SYSTEM_CLOCK) -> None:
        self._clock = clock

    async def send(self, request: object, ctx: object) -> AsyncResponse:
        stamped = request.with_time(self._clock.now())
        for header in stamped.headers:
            stamped.mark(header)
        return await self.next.send(stamped, ctx)
'''

# The same async twin with one seeded semantic drift: it stamps ``now() + 1``
# instead of ``now()``. Everything else is identical.
_DRIFT_ASYNC = _CLEAN_ASYNC.replace(
    "request.with_time(self._clock.now())",
    "request.with_time(self._clock.now() + 1)",
)

# Token map for the fixtures above: the declared async-to-sync swaps the pair
# relies on, including the fixture's own class-name twin.
_FIXTURE_TOKENS: dict[str, str] = {
    "AsyncPolicy": "Policy",
    "async_policy": "policy",
    "AsyncResponse": "Response",
    "AsyncClock": "Clock",
    "ASYNC_SYSTEM_CLOCK": "SYSTEM_CLOCK",
    "AsyncSamplePolicy": "SamplePolicy",
}


def _load_tool() -> ModuleType:
    """Import ``tools/parity_check.py`` by file path."""
    spec = importlib.util.spec_from_file_location("_parity_check", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load parity tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the module's ``@dataclass`` can resolve its
    # own module namespace (``sys.modules[cls.__module__]``) during class build.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    """Provide the loaded parity-check tool once per module."""
    return _load_tool()


@pytest.fixture
def fixture_tokens() -> dict[str, str]:
    """The declared token map the sync/async fixtures are paired under."""
    return dict(_FIXTURE_TOKENS)


def test_clean_pair_is_in_parity(tool: ModuleType, fixture_tokens: dict[str, str]) -> None:
    result = tool.check_parity_sources(
        _CLEAN_SYNC, _CLEAN_ASYNC, token_map=fixture_tokens, mode=tool.Mode.BLOCKING
    )
    assert result.in_parity, result.diff
    assert result.diff == ""
    assert not result.failed


def test_seeded_drift_fails_in_blocking_mode(
    tool: ModuleType, fixture_tokens: dict[str, str]
) -> None:
    result = tool.check_parity_sources(
        _CLEAN_SYNC, _DRIFT_ASYNC, token_map=fixture_tokens, mode=tool.Mode.BLOCKING
    )
    assert not result.in_parity
    assert result.failed
    assert result.diff  # a structural diff artifact is produced


def test_seeded_drift_is_reported_but_not_failed_in_report_only(
    tool: ModuleType, fixture_tokens: dict[str, str]
) -> None:
    result = tool.check_parity_sources(
        _CLEAN_SYNC, _DRIFT_ASYNC, token_map=fixture_tokens, mode=tool.Mode.REPORT_ONLY
    )
    assert not result.in_parity
    assert not result.failed  # report-only never fails the build
    assert result.diff


def test_diff_is_structural_not_textual(tool: ModuleType, fixture_tokens: dict[str, str]) -> None:
    # Reformatting and comment/docstring changes must not register as drift.
    reflowed_async = _CLEAN_ASYNC.replace(
        '"""Async twin of the sample module."""',
        '"""Wildly different prose here."""',
    ).replace("        stamped = request", "        stamped = (request)")
    result = tool.check_parity_sources(_CLEAN_SYNC, reflowed_async, token_map=fixture_tokens)
    assert result.in_parity, result.diff


def test_asyncio_sleep_maps_to_sync_wait(tool: ModuleType) -> None:
    sync_src = "def wait(x: float) -> None:\n    time.sleep(x)\n"
    async_src = "async def wait(x: float) -> None:\n    await asyncio.sleep(x)\n"
    result = tool.check_parity_sources(sync_src, async_src)
    assert result.in_parity, result.diff


def test_async_for_and_with_map_to_sync(tool: ModuleType) -> None:
    sync_src = (
        "def drain(src: object) -> None:\n"
        "    with src.open() as handle:\n"
        "        for chunk in handle.iter():\n"
        "            handle.close()\n"
    )
    async_src = (
        "async def drain(src: object) -> None:\n"
        "    async with src.open() as handle:\n"
        "        async for chunk in handle.aiter():\n"
        "            await handle.aclose()\n"
    )
    result = tool.check_parity_sources(sync_src, async_src)
    assert result.in_parity, result.diff


def test_unmapped_rename_is_caught_as_drift(tool: ModuleType) -> None:
    # An identifier the token map does not name is compared as-is: an accidental
    # rename is drift, never smoothed over by fuzzy matching.
    sync_src = "def f() -> None:\n    handle.commit()\n"
    async_src = "async def f() -> None:\n    await handle.flush()\n"
    result = tool.check_parity_sources(sync_src, async_src)
    assert not result.in_parity


def test_keep_docstrings_surfaces_prose_drift(tool: ModuleType) -> None:
    sync_src = 'def f() -> None:\n    """One."""\n    return None\n'
    async_src = 'async def f() -> None:\n    """Two."""\n    return None\n'
    stripped = tool.check_parity_sources(sync_src, async_src)
    assert stripped.in_parity
    kept = tool.check_parity_sources(sync_src, async_src, keep_docstrings=True)
    assert not kept.in_parity


def test_exact_string_constant_matching_token_is_renamed(tool: ModuleType) -> None:
    # ``__all__`` entries are name-as-string references: a whole-string match to
    # a token key is normalized so the export lists line up.
    sync_src = '__all__ = ["Response"]\n'
    async_src = '__all__ = ["AsyncResponse"]\n'
    assert tool.check_parity_sources(sync_src, async_src).in_parity


def test_substring_of_token_in_string_is_left_as_drift(tool: ModuleType) -> None:
    # Only exact whole-string matches are renamed; a message that merely embeds
    # a token still registers real drift.
    sync_src = 'x = "Response received"\n'
    async_src = 'x = "AsyncResponse received"\n'
    assert not tool.check_parity_sources(sync_src, async_src).in_parity


def test_real_set_date_twin_is_certified_in_parity(tool: ModuleType) -> None:
    # A real hand-written twin that is a faithful mechanical transliteration is
    # certified clean under the default token map — even in blocking mode.
    sync_path = _POLICIES / "set_date.py"
    async_path = _POLICIES / "async_set_date.py"
    if not (sync_path.exists() and async_path.exists()):
        pytest.skip("set_date twin pair not present")
    result = tool.check_parity(sync_path, async_path, mode=tool.Mode.BLOCKING)
    assert result.in_parity, result.diff


@pytest.mark.parametrize(
    ("sync_name", "async_name"),
    [
        ("set_date.py", "async_set_date.py"),
        ("client_identity.py", "async_client_identity.py"),
        ("redirect.py", "async_redirect.py"),
        ("retry.py", "async_retry.py"),
        ("idempotency.py", "async_idempotency.py"),
    ],
)
def test_report_only_over_real_twins_never_fails(
    tool: ModuleType, sync_name: str, async_name: str
) -> None:
    sync_path = _POLICIES / sync_name
    async_path = _POLICIES / async_name
    if not (sync_path.exists() and async_path.exists()):
        pytest.skip(f"policy twin pair not present: {sync_name}/{async_name}")
    result = tool.check_parity(sync_path, async_path, mode=tool.Mode.REPORT_ONLY)
    # Report-only tolerates the legitimate divergence of the hand-written twins
    # (delegating wrappers, async-only helpers); it must never fail the build.
    assert not result.failed
    assert result.mode is tool.Mode.REPORT_ONLY


def test_report_only_produces_a_diff_artifact(tool: ModuleType, tmp_path: Path) -> None:
    # The redirect twin legitimately diverges (the async form delegates to a
    # wrapped sync policy), so report-only over it yields a non-empty artifact.
    sync_path = _POLICIES / "redirect.py"
    async_path = _POLICIES / "async_redirect.py"
    if not (sync_path.exists() and async_path.exists()):
        pytest.skip("redirect twin pair not present")
    out = tmp_path / "parity_diff.txt"
    rc = tool.main(
        [
            str(sync_path),
            str(async_path),
            "--mode",
            "report-only",
            "--report-out",
            str(out),
        ]
    )
    assert rc == 0  # report-only never fails the build
    assert out.exists()
    assert out.read_text(encoding="utf-8")  # a diff artifact was written


def _write_pair(tmp_path: Path, sync_src: str, async_src: str) -> tuple[Path, Path, Path]:
    """Write a sync/async pair plus a JSON token map to ``tmp_path``."""
    sync_file = tmp_path / "sync_mod.py"
    async_file = tmp_path / "async_mod.py"
    token_file = tmp_path / "tokens.json"
    sync_file.write_text(sync_src, encoding="utf-8")
    async_file.write_text(async_src, encoding="utf-8")
    token_file.write_text(json.dumps(_FIXTURE_TOKENS), encoding="utf-8")
    return sync_file, async_file, token_file


def test_cli_blocking_mode_exits_nonzero_on_drift(tool: ModuleType, tmp_path: Path) -> None:
    sync_file, async_file, token_file = _write_pair(tmp_path, _CLEAN_SYNC, _DRIFT_ASYNC)
    rc = tool.main(
        [str(sync_file), str(async_file), "--mode", "blocking", "--token-map", str(token_file)]
    )
    assert rc == 1


def test_cli_blocking_mode_passes_clean_pair(tool: ModuleType, tmp_path: Path) -> None:
    sync_file, async_file, token_file = _write_pair(tmp_path, _CLEAN_SYNC, _CLEAN_ASYNC)
    rc = tool.main(
        [str(sync_file), str(async_file), "--mode", "blocking", "--token-map", str(token_file)]
    )
    assert rc == 0
