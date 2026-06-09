# Contributing

Thanks for your interest in the Dexpace Python SDK. External pull requests
are welcome — this page covers everything you need to get a change merged.

## Setup

The repository is a [`uv`](https://docs.astral.sh/uv/)-managed workspace of
five packages. One sync provisions everything in editable mode along with
the dev toolchain:

```bash
git clone https://github.com/dexpace/python-sdk.git
cd python-sdk
uv sync
```

## Quality gates

Every pull request must pass the same four gates CI runs (on Python 3.12,
3.13, and 3.14):

```bash
uv run pytest -q                 # full test suite
uv run mypy --strict             # type-check
uv run ruff check                # lint
uv run ruff format --check       # formatting
```

Run them locally before opening a PR.

## Conventions

The full convention set lives in [`CLAUDE.md`](CLAUDE.md). The essentials:

- **Python 3.12+** with modern syntax: `X | None`, built-in generics,
  PEP 695 type parameters, `from __future__ import annotations` everywhere.
- **Immutable models**: `@dataclass(frozen=True, slots=True)`; mutate via
  `dataclasses.replace` or `with_*` helpers — no builders.
- **`Protocol` for SPIs, `ABC` for shared behaviour.**
- **No new runtime dependencies.** `core` ships against the standard
  library plus `furl` only; new third-party needs belong behind the
  `HttpClient` or `Serde` seams, or in a new transport package.
- **Google-style docstrings** on every public symbol; functions capped at
  50 lines.
- **MIT licence header** (two lines) at the top of every `.py` file, src
  and tests alike.

## Commit messages

Use the prefixes the history already follows:

| Prefix   | Use for                          |
|----------|----------------------------------|
| `feat:`  | new features                     |
| `fix:`   | bug fixes                        |
| `chore:` | refactors and cleanup            |
| `docs:`  | documentation-only changes       |
| `ci:`    | CI configuration                 |

## Reporting issues

Use the [issue templates](https://github.com/dexpace/python-sdk/issues/new/choose).
For security vulnerabilities, follow [`SECURITY.md`](SECURITY.md) instead of
opening a public issue.
