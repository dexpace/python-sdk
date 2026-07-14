# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Prove the golden corpus ships as package data and loads from the install.

The corpus must load through ``importlib.resources`` from the installed
``dexpace.sdk.tck`` distribution — never from a repo-relative path — so a
consumer that ``pip install``s the wheel and runs ``pytest --pyargs
dexpace.sdk.tck`` from any working directory gets the exact fixture bytes. These
tests read the corpus as package-data resources and, as an external-invocation
smoke, load it in a subprocess whose working directory is *outside* the repo
tree.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from dexpace.sdk.tck.golden_corpus import FAMILIES, load_all

_CORPUS_ANCHOR = "dexpace.sdk.tck.golden_corpus"


def test_every_family_loads_as_package_data() -> None:
    corpus = load_all()
    assert set(corpus) == set(FAMILIES)
    for family in FAMILIES:
        assert corpus[family], f"{family} loaded empty from package data"


def test_binary_fixture_bytes_survive_package_data_loading() -> None:
    # A CRLF-terminated SSE fixture is the sharpest proof that the resource is
    # read as raw bytes (never text-mode) straight out of the packaged data.
    resource = files(_CORPUS_ANCHOR).joinpath("data", "sse", "crlf-single-event.sse")
    assert resource.read_bytes() == b"data: hello\r\n\r\n"


def test_corpus_loads_from_outside_the_repo_tree(tmp_path: Path) -> None:
    # Run from a cwd outside the repository so nothing repo-relative can be in
    # play: the corpus must be found purely through the installed package.
    script = (
        "from dexpace.sdk.tck.golden_corpus import FAMILIES, load_all\n"
        "corpus = load_all()\n"
        "assert set(corpus) == set(FAMILIES), corpus.keys()\n"
        "assert all(corpus[f] for f in FAMILIES)\n"
        "print('OK', sum(len(corpus[f]) for f in FAMILIES))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK ")
