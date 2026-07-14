# Quality gates (non-functional requirements)

Every non-functional requirement for the SDK is wired as an automatic,
**blocking** CI check, or explicitly recorded as not applicable. Nothing here is
advisory: a red gate fails the build. The checks live in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and their thresholds
live in the workspace root [`pyproject.toml`](../pyproject.toml).

## Status summary

| # | Requirement | Status | Where it is enforced |
|---|-------------|--------|----------------------|
| 1 | Aggregate line **and** branch coverage ≥ 80% | Enforced | `coverage` job → `pytest --cov`; floor is `[tool.coverage.report].fail_under = 80` |
| 2 | Warnings-as-errors, including `DeprecationWarning`, on the first-party suites | Enforced | `test` job → `pytest`; `[tool.pytest.ini_options].filterwarnings` (`error` + explicit `error::DeprecationWarning`) |
| 3 | `ruff` lint fatal, with only scoped, documented waivers | Enforced | `test` job → `ruff check`; see "Lint waivers" below |
| 4 | `ruff format` consistency | Enforced | `test` job → `ruff format --check` |
| 5 | `mypy --strict` clean; no `Any` in the public API; no unused `# type: ignore` | Enforced | `test` job → `mypy --strict` (`warn_unused_ignores = true`) |
| 6 | Runtime dependency budget: core = stdlib + `furl`; each adapter = core + ≤ 1 third-party lib | Enforced | `packaging` job → `tools/check_dependency_audit.py`; reinforced by the `import-linter` contracts |
| 7 | Import-direction, `furl` confinement, and pipeline layering invariants | Enforced | `test` job → `lint-imports` (`.importlinter`) |
| 8 | PEP 420 namespace-package layout (no stray `__init__.py` on shared prefixes) | Enforced | `packaging` job → `tools/check_namespace_packages.py` |
| 9 | MIT licence header on every `.py` file | Enforced | `packaging` job → `tools/check_mit_headers.py` |
| 10 | Wheels build and import cleanly in a fresh environment | Enforced | `packaging` job → wheel build + `tools/smoke_wheel_import.py` |
| 11 | Supported interpreter matrix (CPython 3.12 – 3.14) | Enforced | `test` job → `strategy.matrix.python-version` |
| 12 | PEP 702 `@warnings.deprecated` markers carry the right runtime shim (3.12 via `typing_extensions`, native 3.13+) | N/A | Nothing in the SDK is deprecated yet — no `@deprecated` markers exist, so there is nothing to gate. Revisit when the first deprecation lands. |
| 13 | JVM-shaped tuning knobs (bytecode shrinker, GC/heap tuning, JIT flags) | Not applicable to Python | These are Java/JVM build concerns with no CPython analogue; there is nothing to implement or gate. |

## Coverage gate

`pytest-cov` measures line and branch coverage over the `dexpace.sdk` source
tree (`[tool.coverage.run]`: `branch = true`, `source = ["dexpace.sdk"]`). The
blocking floor is `fail_under = 80`; `pytest-cov` exits non-zero when the
aggregate total drops below it. The tree currently measures **~94%**
(93.6% after the standard `TYPE_CHECKING` / `__main__` exclusions), so 80% is a
regression floor, not a target to code down to.

The gate runs in its own `coverage` job (Python 3.12) rather than inside the
matrix `test` job, so the existing per-version steps are untouched and the
coverage number has a single, unambiguous source.

## Warnings gate

The first-party suites run under `filterwarnings = ["error", ...]`, which
promotes **every** warning category to an error. `error::DeprecationWarning` is
listed explicitly as well, so the `-W error::DeprecationWarning` requirement is
pinned literally and survives any future narrowing of the catch-all `error`
entry. The single scoped waiver, `ignore::DeprecationWarning:pytest_asyncio.*`,
suppresses deprecation warnings raised from inside the `pytest-asyncio` plugin
only; it is listed last so it takes precedence for that source alone, and
first-party deprecations still fail the run.

## Lint waivers

`ruff check` is fatal. Blanket suppression is structurally prevented rather than
policed by convention:

- `RUF100` (part of the selected `RUF` group) fails the build on any unused or
  redundant `# noqa`, so a `# noqa` only survives if it suppresses a real,
  currently-firing diagnostic.
- Every in-tree `# noqa` names an explicit rule code (e.g. `# noqa: N802`); no
  bare, code-less `# noqa` exists anywhere in the sources.
- Rule-set-wide ignores are confined to `[tool.ruff.lint.ignore]`
  (`E501`, `B008`) and narrowly-scoped `per-file-ignores`, each carrying a
  comment explaining why.

The `# type: ignore` story mirrors this: `mypy`'s `warn_unused_ignores = true`
rejects any ignore that no longer suppresses an error, and every ignore in the
tree carries an explicit error code (e.g. `# type: ignore[arg-type]`).
