# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Guard the single version train shared by every published SDK distribution.

The workspace ships six distributions (`dexpace-sdk-core` plus four transport
adapters and the conformance kit). They release together on one version train:
every published package carries the same version, and that version is the one
source of truth recorded in each package's ``pyproject.toml`` and mirrored in
``uv.lock``. The workspace root (``dexpace-sdk-workspace``) is intentionally not
published and keeps the ``0.0.0`` sentinel, so it is excluded from the train.

This gate makes two invariants mechanical instead of relying on release-time
discipline:

* every published package sits at one identical, real version (a bump touches
  all of them or the gate fails), and that version is mirrored in ``uv.lock``;
  and
* a built wheel's metadata carries that real version -- never a build-time
  placeholder such as ``0.0.0`` or ``unknown`` -- so a shipped artifact can
  never advertise an anonymous build.

Run it against the working tree:

    uv run --no-project python tools/check_version_train.py

Add ``--wheel-dir dist`` to additionally assert that every built wheel in a
directory carries the train version:

    uv run --no-project python tools/check_version_train.py --wheel-dir dist

Exit code is ``0`` when the train is consistent and ``1`` on the first breach.
Only the standard library is used so the script runs in a minimal environment.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import zipfile
from pathlib import Path

__all__ = [
    "ANCHOR_PACKAGE",
    "PLACEHOLDER_VERSIONS",
    "PUBLISHED_PACKAGES",
    "check_train",
    "check_wheels",
    "lock_versions",
    "main",
    "package_versions",
    "repo_root",
    "wheel_version",
]

#: The distributions that release together on the shared version train. The
#: workspace root is deliberately absent -- it is never published.
PUBLISHED_PACKAGES: tuple[str, ...] = (
    "dexpace-sdk-core",
    "dexpace-sdk-http-stdlib",
    "dexpace-sdk-http-httpx",
    "dexpace-sdk-http-aiohttp",
    "dexpace-sdk-http-requests",
    "dexpace-sdk-tck",
)

#: The package whose version anchors the train; every other package must match.
ANCHOR_PACKAGE = "dexpace-sdk-core"

#: Versions that signal an unstamped / placeholder build rather than a release.
PLACEHOLDER_VERSIONS: frozenset[str] = frozenset({"", "0", "0.0", "0.0.0", "0.0.0.dev0", "unknown"})

# A pragmatic PEP 440 release matcher (release segment plus optional pre/post/
# dev/local suffixes). Stdlib-only: the ``packaging`` module is not guaranteed
# to be importable under ``uv run --no-project``.
_PEP440 = re.compile(
    r"^\d+(\.\d+)*"
    r"((a|b|rc)\d+)?"
    r"(\.post\d+)?"
    r"(\.dev\d+)?"
    r"(\+[a-z0-9]+(\.[a-z0-9]+)*)?$",
    re.IGNORECASE,
)


def repo_root() -> Path:
    """Return the repository root (the parent of this ``tools`` directory)."""
    return Path(__file__).resolve().parent.parent


def _project_version(pyproject: Path) -> str:
    """Read ``[project].version`` from a ``pyproject.toml``."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def package_versions(root: Path) -> dict[str, str]:
    """Return each published package's declared version, keyed by name.

    Args:
        root: Repository root containing the ``packages`` directory.

    Returns:
        A mapping from distribution name to the version declared in its
        ``pyproject.toml``.
    """
    return {
        name: _project_version(root / "packages" / name / "pyproject.toml")
        for name in PUBLISHED_PACKAGES
    }


def lock_versions(root: Path) -> dict[str, str]:
    """Return the version ``uv.lock`` records for each published package.

    Args:
        root: Repository root containing ``uv.lock``.

    Returns:
        A mapping from distribution name to its locked version, restricted to
        the published packages.
    """
    data = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    published = set(PUBLISHED_PACKAGES)
    return {
        str(pkg["name"]): str(pkg["version"])
        for pkg in data.get("package", [])
        if str(pkg.get("name")) in published
    }


def check_train(pyproject: dict[str, str], lock: dict[str, str]) -> list[str]:
    """Check that every published package sits at one real, matching version.

    Args:
        pyproject: Versions declared in each package's ``pyproject.toml``.
        lock: Versions recorded for each package in ``uv.lock``.

    Returns:
        A list of human-readable failure messages; empty when the train is
        consistent.
    """
    failures: list[str] = []
    train = pyproject[ANCHOR_PACKAGE]
    if train in PLACEHOLDER_VERSIONS or not _PEP440.match(train):
        failures.append(f"train anchor {ANCHOR_PACKAGE} has a non-release version {train!r}")
    for name in PUBLISHED_PACKAGES:
        if pyproject[name] != train:
            failures.append(f"{name} pyproject version {pyproject[name]!r} != train {train!r}")
        locked = lock.get(name)
        if locked is None:
            failures.append(f"{name} is absent from uv.lock")
        elif locked != pyproject[name]:
            failures.append(f"{name} uv.lock version {locked!r} != pyproject {pyproject[name]!r}")
    return failures


def wheel_version(wheel: Path) -> str:
    """Return the ``Version`` field from a built wheel's metadata.

    Args:
        wheel: Path to a ``.whl`` file.

    Returns:
        The version string recorded in the wheel's ``*.dist-info/METADATA``.

    Raises:
        RuntimeError: If the wheel has no metadata or no ``Version`` field.
    """
    with zipfile.ZipFile(wheel) as archive:
        meta_names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if not meta_names:
            raise RuntimeError(f"{wheel.name}: no .dist-info/METADATA in wheel")
        text = archive.read(meta_names[0]).decode("utf-8")
    for line in text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"{wheel.name}: METADATA has no Version field")


def check_wheels(wheel_dir: Path, expected: str) -> list[str]:
    """Check that every wheel in a directory carries the expected real version.

    Args:
        wheel_dir: Directory holding built ``.whl`` files.
        expected: The train version each wheel must advertise.

    Returns:
        A list of failure messages; empty when every wheel matches and none
        carries a placeholder version.
    """
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        return [f"no wheels found in {wheel_dir}"]
    failures: list[str] = []
    for wheel in wheels:
        version = wheel_version(wheel)
        if version in PLACEHOLDER_VERSIONS:
            failures.append(f"{wheel.name} carries placeholder version {version!r}")
        elif version != expected:
            failures.append(f"{wheel.name} version {version!r} != train {expected!r}")
    return failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description="Verify the single SDK version train.")
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        default=None,
        help="Directory of built wheels whose metadata version must match the train.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Verify the version train and return a process exit code.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        ``0`` when the train is consistent, ``1`` otherwise.
    """
    args = _parse_args(argv)
    root = repo_root()
    pyproject = package_versions(root)
    failures = check_train(pyproject, lock_versions(root))
    if not failures and args.wheel_dir is not None:
        failures += check_wheels(args.wheel_dir, pyproject[ANCHOR_PACKAGE])
    if failures:
        print("version train check: FAILED")
        for message in failures:
            print(f"  {message}")
        return 1
    train = pyproject[ANCHOR_PACKAGE]
    scope = "pyproject + uv.lock"
    if args.wheel_dir is not None:
        scope += " + wheel metadata"
    print(f"version train check: OK (all published packages at {train}; {scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
