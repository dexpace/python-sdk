# dexpace-sdk-tck

The executable **conformance kit** (TCK) for
[`dexpace-sdk-core`](https://pypi.org/project/dexpace-sdk-core/) transport
adapters. It houses the shared, transport-**agnostic** conformance battery, the
golden wire-exactness byte corpus, and the adapter-certification seam, so any
transport adapter can be certified against one shared spec.

This is a **test-only** distribution. Its runtime dependency is
`dexpace-sdk-core`; `pytest` / `hypothesis` are declared under the `test` extra
because they are how the kit is *run*, not what its library surface depends on.

## What ships here

- **Harness ABCs** (`dexpace.sdk.tck.harness`) — `SyncTransportHarness` /
  `AsyncTransportHarness` plus the neutral programming/observation seam
  (`Outcome`, `ResponseSpec`, `SentRequest`, `DropLog`, capability flags) and
  the `SYNC_HARNESS_FACTORIES` / `ASYNC_HARNESS_FACTORIES` registries. A
  transport adapter is certified by implementing one harness and appending its
  factory to the registry.
- **In-memory reference** (`dexpace.sdk.tck.fake_transport`) — a reference
  adapter over a fake native layer that satisfies the whole battery, so the
  battery is a genuine contract rather than a tautology.
- **The battery** (`test_sync_contract` / `test_async_contract`) — every test
  drives a parametrized harness and never a concrete transport.
- **The golden corpus** (`dexpace.sdk.tck.golden_corpus`) — checked-in raw byte
  fixtures (URL/query codec, redirect `Location` resolution, raw-query
  pagination splices, SSE streams, `Retry-After` pacing, RFC 1123 dates) plus a
  loader that reads them via `importlib.resources` in binary mode (zip-safe), so
  CR / LF / CRLF and percent-encoded bytes are never mangled.

The kit depends only on core's **public re-exported surface**; it imports no
underscore-prefixed (private) core module. An in-package test
(`test_import_discipline`) enforces that invariant by AST-scanning the kit's own
import graph.

## Certifying an adapter

Each transport-adapter package registers its own harness from its
`tests/conftest.py`:

```python
from dexpace.sdk.tck.harness import SYNC_HARNESS_FACTORIES

from my_harness import MyTransportSyncHarness

SYNC_HARNESS_FACTORIES.append(("my-transport", MyTransportSyncHarness))
```

When the workspace test session collects core plus every adapter package, the
battery runs against the in-memory reference and all registered adapters in one
pass. The kit can also be invoked directly against whatever adapters have
registered:

```bash
pytest --pyargs dexpace.sdk.tck
```

## Installation

```bash
pip install "dexpace-sdk-tck[test]"
```

This pulls in `dexpace-sdk-core` plus the `pytest` / `hypothesis` needed to run
the battery.
