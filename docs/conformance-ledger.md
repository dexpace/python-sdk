# Conformance ledger

Machine-generated record of how this Python port meets every normative requirement
in the consolidated requirement index (the product spec Appendix C, vendored as
tools/requirement_index.json). Regenerate with tools/build_requirement_index.py +
a gated pytest collection after the index or coverage changes. Each requirement is
in one state:

- **test** — a test tagged @pytest.mark.req(<id>) asserts the behaviour.
- **deviation** — provided by a documented, different mechanism (rationale in the row).
- **n/a** — not applicable to the Python port (a Java/JVM idiom or a deliberately
  omitted seam, e.g. the byte-stream provider seam or the Builder pattern).
- **uncovered** — a SHOULD/MAY with no dedicated test; non-gating by design.

**Totals (645 requirements):** 505 test-covered · 43 deviation · 55 n/a · 42 uncovered SHOULD/MAY (non-gating).

**MUST-level (532):** 434 test-covered · 98 accounted as deviation/n-a · **0 unaccounted**.

The blocking CI traceability gate (tools/traceability_audit.py) fails on any
unaccounted MUST across every subsystem, so the MUST-unaccounted count is held at zero.


## async

| ID | Level | Status | Evidence |
|---|---|---|---|
| ASYNC-1 | MUST | test | 8 test(s) |
| ASYNC-2 | MUST | test | 8 test(s) |
| ASYNC-3 | MUST | n/a | cancel-with-interrupt vs cancel-without-interrupt is JVM Thread.interrupt() worker-thread semantics; CPython threads are not interruptible from outside (deviations.md sync-cancella |
| ASYNC-4 | MUST | n/a | Ordered interrupt-flag delivery to avoid poisoning a pooled thread is a JVM interrupt-flag hazard; CPython has no cross-thread interrupt flag, so the poisoning failure mode does no |
| ASYNC-5 | MUST | test | 5 test(s) |
| ASYNC-6 | MUST | test | 6 test(s) |
| ASYNC-7 | SHOULD | test | 5 test(s) |
| ASYNC-8 | SHOULD | test | 2 test(s) |
| ASYNC-9 | MUST | test | 2 test(s) |
| ASYNC-10 | MUST | test | 2 test(s) |
| ASYNC-11 | MUST | test | 2 test(s) |
| ASYNC-12 | MUST | test | 1 test(s) |
| ASYNC-13 | MUST | test | 3 test(s) |
| ASYNC-14 | MUST | test | 3 test(s) |
| ASYNC-15 | MUST | test | 7 test(s) |
| ASYNC-16 | SHOULD | test | 2 test(s) |
| ASYNC-17 | SHOULD | uncovered | SHOULD-level, non-gating |
| ASYNC-18 | MUST | deviation | The non-blocking async delay is provided by stdlib asyncio.sleep via AsyncClock.sleep (_AsyncSystemClock.sleep / async_http_client.asyncio_sleep) — an event-loop timer that holds n |
| ASYNC-19 | MUST | test | 3 test(s) |
| ASYNC-20 | MUST | test | 1 test(s) |
| ASYNC-21 | MUST | n/a | Exposing SSE 'as a reactive stream' is a JVM reactive-streams/Reactor Publisher ecosystem adapter; this port has no reactive-streams seam and instead exposes SSE as a native async |
| ASYNC-22 | MUST | test | 4 test(s) |

## auth

| ID | Level | Status | Evidence |
|---|---|---|---|
| AUTH-1 | MUST | deviation | The port's resolver (codegen/auth.py) recognizes open scheme-name strings matched against an AuthRegistry rather than a fixed {OAUTH2,API_KEY,BASIC,DIGEST,NO_AUTH} enum, and models |
| AUTH-2 | MUST | deviation | AuthRequirement is a frozen (immutable, value-equal) dataclass binding exactly one scheme to its scopes (test_auth_scheme_satisfies_by_name_and_scope_subset), but it drops the sepa |
| AUTH-3 | MUST | deviation | AuthDescriptor is a non-empty, ordered, immutable frozen dataclass that rejects an empty requirement list at construction (test_auth_descriptor_rejects_empty), but the allowsAnonym |
| AUTH-4 | MUST | test | 3 test(s) |
| AUTH-5 | MUST | test | 2 test(s) |
| AUTH-6 | MUST | deviation | A present-but-unsatisfiable selected tier raises the distinct AuthResolutionError (test_unsatisfiable_per_call_does_not_fall_through_to_client), but when all three descriptors are |
| AUTH-7 | MUST | test | 2 test(s) |
| AUTH-8 | MUST | test | 3 test(s) |
| AUTH-9 | MUST | test | 2 test(s) |
| AUTH-10 | MUST | deviation | Additive grace-margin expiry evaluation is implemented and tested (needs_refresh/classify/is_expired), but AccessTokenInfo makes expires_on a mandatory int so there is no null-mean |
| AUTH-11 | MUST | test | 2 test(s) |
| AUTH-12 | MUST | deviation | The RFC 7235 parser handles multiple comma-separated challenges, quoted-string commas/=, backslash escapes, lower-cased parameter names and verbatim values (heavily tested), but sc |
| AUTH-13 | MUST | test | 2 test(s) |
| AUTH-14 | MUST | test | 3 test(s) |
| AUTH-15 | MUST | test | 3 test(s) |
| AUTH-16 | MUST | test | 3 test(s) |
| AUTH-17 | MUST | test | 2 test(s) |
| AUTH-18 | MUST | test | 3 test(s) |
| AUTH-19 | SHOULD | test | 3 test(s) |
| AUTH-20 | MUST | test | 3 test(s) |
| AUTH-21 | MUST | test | 3 test(s) |
| AUTH-22 | MUST | test | 3 test(s) |
| AUTH-23 | MUST | test | 2 test(s) |
| AUTH-24 | MUST | test | 1 test(s) |
| AUTH-25 | MUST | test | 2 test(s) |
| AUTH-26 | MUST | test | 3 test(s) |
| AUTH-27 | MUST | test | 2 test(s) |
| AUTH-28 | MUST | test | 3 test(s) |
| AUTH-29 | MUST | test | 3 test(s) |
| AUTH-30 | MUST | test | 2 test(s) |
| AUTH-31 | MUST | test | 2 test(s) |
| AUTH-32 | MUST | test | 3 test(s) |
| AUTH-33 | MUST | test | 2 test(s) |
| AUTH-34 | MUST | test | 3 test(s) |
| AUTH-35 | MUST | test | 3 test(s) |
| AUTH-36 | MUST | test | 2 test(s) |
| AUTH-37 | MUST | test | 5 test(s) |
| AUTH-38 | SHOULD | test | 2 test(s) |

## bodies

| ID | Level | Status | Evidence |
|---|---|---|---|
| BODY-1 | MUST | test | 3 test(s) |
| BODY-2 | MUST | test | 4 test(s) |
| BODY-3 | MUST | test | 5 test(s) |
| BODY-4 | MUST | test | 3 test(s) |
| BODY-5 | MUST | test | 3 test(s) |
| BODY-6 | MUST | test | 4 test(s) |
| BODY-7 | MUST | test | 2 test(s) |
| BODY-8 | MUST | test | 2 test(s) |
| BODY-9 | SHOULD | uncovered | SHOULD-level, non-gating |
| BODY-10 | MUST | n/a | The exact-length stream-to-sink copy primitive (with premature-EOF and zero-byte-read errors) belongs to the removed Io/byte-stream seam; the port's stream body iterates to natural |
| BODY-11 | MUST | test | 3 test(s) |
| BODY-12 | SHOULD | test | 1 test(s) |
| BODY-13 | MUST | test | 2 test(s) |
| BODY-14 | MUST | test | 2 test(s) |
| BODY-15 | MUST | test | 2 test(s) |
| BODY-16 | MUST | test | 2 test(s) |
| BODY-17 | MUST | test | 3 test(s) |
| BODY-18 | MUST | test | 3 test(s) |
| BODY-19 | MUST | test | 2 test(s) |
| BODY-20 | SHOULD | uncovered | SHOULD-level, non-gating |
| BODY-21 | MUST | test | 2 test(s) |
| BODY-22 | MUST | test | 2 test(s) |
| BODY-23 | MUST | test | 2 test(s) |
| BODY-24 | MUST | test | 2 test(s) |
| BODY-25 | MUST | deviation | In the iterator-based ResponseBody model (the removed read-into-buffer Io seam) end-of-stream is signalled only by iterator exhaustion, so an interior zero-length chunk is tolerate |
| BODY-26 | MUST | test | 3 test(s) |
| BODY-27 | MUST | test | 2 test(s) |
| BODY-28 | MUST | test | 2 test(s) |
| BODY-29 | SHOULD | test | 2 test(s) |
| BODY-30 | MUST | test | 3 test(s) |
| BODY-31 | MUST | test | 13 test(s) |
| BODY-32 | MUST | test | 2 test(s) |
| BODY-33 | SHOULD | test | 3 test(s) |
| BODY-34 | MUST | test | 2 test(s) |
| BODY-35 | MUST | test | 3 test(s) |
| BODY-36 | MAY | uncovered | MAY-level, non-gating |
| BODY-37 | MUST | n/a | A tee sink exposing a writable buffer handle that bypasses the primary sink is an Okio Sink/BufferedSink API concept from the removed Io/byte-stream seam (CLAUDE.md forbids reintro |

## config

| ID | Level | Status | Evidence |
|---|---|---|---|
| CFG-1 | MUST | test | 7 test(s) |
| CFG-2 | MUST | test | 7 test(s) |
| CFG-3 | MUST | test | 2 test(s) |
| CFG-4 | MUST | test | 2 test(s) |
| CFG-5 | MUST | test | 3 test(s) |
| CFG-6 | MUST | test | 14 test(s) |
| CFG-7 | MUST | deviation | ISO-8601, shorthand ms/s/m/h/d, negative-rejection and unknown-unit rejection are all implemented and tested, but a bare number is deliberately interpreted as SECONDS (not the spec |
| CFG-8 | MUST | test | 2 test(s) |
| CFG-9 | MUST | test | 4 test(s) |
| CFG-10 | MUST | test | 7 test(s) |
| CFG-11 | MUST | test | 3 test(s) |
| CFG-12 | SHOULD | test | 1 test(s) |
| CFG-13 | SHOULD | test | 2 test(s) |
| CFG-14 | SHOULD | uncovered | SHOULD-level, non-gating |
| CFG-15 | MUST | test | 3 test(s) |
| CFG-16 | MUST | test | 2 test(s) |
| CFG-17 | MUST | test | 4 test(s) |
| CFG-18 | SHOULD | uncovered | SHOULD-level, non-gating |
| CFG-19 | SHOULD | uncovered | SHOULD-level, non-gating |
| CFG-20 | SHOULD | uncovered | SHOULD-level, non-gating |
| CFG-21 | MUST | n/a | This is a corollary of the interruptible-task future (CFG-20): a cancelled-future-with-closeable-result discard path only exists inside the JVM ExecutorService/Future+thread-interr |
| CFG-22 | MUST | test | 3 test(s) |
| CFG-23 | MUST | test | 3 test(s) |
| CFG-24 | MUST | test | 9 test(s) |
| CFG-25 | MUST | deviation | An out-of-range/non-numeric port yields None with a warning (tested), and the anti-fabrication intent ('never invent a per-scheme default port') is honored; but a genuinely absent |
| CFG-26 | MUST | test | 6 test(s) |
| CFG-27 | MUST | test | 2 test(s) |
| CFG-28 | MAY | test | 2 test(s) |
| CFG-29 | MUST | test | 3 test(s) |
| CFG-30 | MUST | test | 5 test(s) |
| CFG-31 | MUST | deviation | The blank/empty-input MUST-fail clause is satisfied and tested (parse returns None), but parsing delegates to stdlib email.utils.parsedate_to_datetime (documented as tolerant in ht |
| CFG-32 | MUST | n/a | A bespoke non-blocking type-4 UUID generator is a JVM workaround for java.util.UUID.randomUUID()'s blocking SecureRandom; CPython's stdlib uuid.uuid4() is already non-blocking and |
| CFG-33 | MUST | n/a | Deep value-equality helpers are the Java Objects.deepEquals/Arrays.deepEquals/deepHashCode idiom; Python's native structural == and hash on list/tuple/dict already compare by conte |
| CFG-34 | MUST | n/a | The float-array deep-equality semantics (NaN==NaN, +0.0!=-0.0, object-array vs primitive-array kind distinction) are the Java Arrays.equals(double[])/Double.equals contract; Python |
| CFG-35 | SHOULD | uncovered | SHOULD-level, non-gating |
| CFG-36 | SHOULD | test | 3 test(s) |
| CFG-37 | MUST | test | 4 test(s) |
| CFG-38 | MUST | test | 3 test(s) |

## context

| ID | Level | Status | Evidence |
|---|---|---|---|
| CTX-1 | MUST | test | 3 test(s) |
| CTX-2 | MUST | test | 2 test(s) |
| CTX-3 | MUST | test | 1 test(s) |
| CTX-4 | MUST | test | 3 test(s) |
| CTX-5 | MUST | deviation | Per the CTX-17 promotion-time model, keys are minted at promotion (ContextStore.new_key), not at direct construction, so an off-chain context defaults to store_key=None (verified: |
| CTX-6 | MUST | test | 2 test(s) |
| CTX-7 | MUST | test | 2 test(s) |
| CTX-8 | MUST | test | 3 test(s) |
| CTX-9 | MUST | deviation | CallContext.close() removes unconditionally by the call-unique shared key (context_store.py 'Two removers coexist' docstring) rather than identity-conditionally, because request an |
| CTX-10 | MUST | deviation | A promoted dispatch stays unregistered (store_key=None) so its close is a no-op as required, but closing a promoted request tier deliberately clears the successor exchange because |
| CTX-11 | MUST | test | 3 test(s) |
| CTX-12 | SHOULD | test | 1 test(s) |
| CTX-13 | MAY | test | 1 test(s) |
| CTX-14 | MUST | test | 3 test(s) |
| CTX-15 | MUST | test | 4 test(s) |
| CTX-16 | SHOULD | uncovered | SHOULD-level, non-gating |
| CTX-17 | MUST | test | 2 test(s) |
| CTX-18 | MUST | test | 3 test(s) |
| CTX-19 | MUST | test | 1 test(s) |
| CTX-20 | SHOULD | test | 3 test(s) |

## http

| ID | Level | Status | Evidence |
|---|---|---|---|
| HTTP-1 | MUST | test | 3 test(s) |
| HTTP-2 | MUST | deviation | Java builders are replaced by frozen dataclasses whose __post_init__ validates on every construction path (raw constructor and dataclasses.replace included), so validation cannot b |
| HTTP-3 | MUST | deviation | newBuilder()-style derivation is provided by the with_* helpers plus dataclasses.replace (derive a modified copy without restating fields), and collections are stored as immutable |
| HTTP-4 | MUST | test | 3 test(s) |
| HTTP-5 | MUST | test | 2 test(s) |
| HTTP-6 | MUST | test | 3 test(s) |
| HTTP-7 | MUST | test | 8 test(s) |
| HTTP-8 | SHOULD | uncovered | SHOULD-level, non-gating |
| HTTP-9 | MUST | test | 12 test(s) |
| HTTP-10 | MUST | test | 5 test(s) |
| HTTP-11 | MUST | test | 28 test(s) |
| HTTP-12 | MUST | test | 4 test(s) |
| HTTP-13 | MUST | test | 6 test(s) |
| HTTP-14 | MUST | test | 3 test(s) |
| HTTP-15 | MUST | test | 1 test(s) |
| HTTP-16 | SHOULD | test | 1 test(s) |
| HTTP-17 | MUST | test | 8 test(s) |
| HTTP-18 | MUST | test | 7 test(s) |
| HTTP-19 | MUST | test | 4 test(s) |
| HTTP-20 | MUST | test | 2 test(s) |
| HTTP-21 | MUST | test | 3 test(s) |
| HTTP-22 | MAY | test | 1 test(s) |
| HTTP-23 | MUST | test | 93 test(s) |
| HTTP-24 | MUST | test | 13 test(s) |
| HTTP-25 | MUST | test | 10 test(s) |
| HTTP-26 | MUST | test | 42 test(s) |
| HTTP-27 | SHOULD | test | 3 test(s) |
| HTTP-28 | MUST | test | 3 test(s) |
| HTTP-29 | MUST | test | 57 test(s) |
| HTTP-30 | MUST | test | 2 test(s) |
| HTTP-31 | MUST | test | 14 test(s) |
| HTTP-32 | SHOULD | test | 3 test(s) |
| HTTP-33 | MUST | test | 12 test(s) |
| HTTP-34 | MUST | deviation | RequestOptions is folded into Request keyword-arg fields (timeout, retries) with None 'use-the-default' sentinels — a default-constructed Request being the canonical 'override noth |
| HTTP-35 | MUST | test | 3 test(s) |
| HTTP-36 | MUST | test | 3 test(s) |
| HTTP-37 | MUST | test | 5 test(s) |
| HTTP-38 | MUST | deviation | Replayability-by-source is honored and tested (bytes/string/file/form replayable; stream/iter single-use), but the form body deliberately uses strict RFC 3986 %20 encoding (never ' |
| HTTP-39 | MUST | n/a | The fixed-count 'copy exactly N bytes from a source, raising on early EOF' primitive is the removed Okio/Io byte-stream (Source) seam; the Python port models stream bodies over std |
| HTTP-40 | MUST | test | 8 test(s) |
| HTTP-41 | MUST | test | 3 test(s) |
| HTTP-42 | MUST | test | 2 test(s) |
| HTTP-43 | MUST | test | 3 test(s) |
| HTTP-44 | MUST | test | 3 test(s) |
| HTTP-45 | MUST | test | 1 test(s) |
| HTTP-46 | MUST | test | 1 test(s) |
| HTTP-47 | SHOULD | test | 2 test(s) |
| HTTP-48 | SHOULD | test | 8 test(s) |
| HTTP-49 | SHOULD | test | 6 test(s) |
| HTTP-50 | SHOULD | test | 7 test(s) |
| HTTP-51 | SHOULD | test | 3 test(s) |
| HTTP-52 | MUST | test | 3 test(s) |
| HTTP-53 | MUST | test | 9 test(s) |

## io

| ID | Level | Status | Evidence |
|---|---|---|---|
| IO-1 | MUST | n/a | Okio Source.read tail-append/return-code contract; the byte-stream Source/Sink/Buffer seam was removed and Python stdlib bytes/bytearray/memoryview/BytesIO/BinaryIO is the contract |
| IO-2 | MUST | n/a | Okio Source.read(0) EOF-vs-zero semantics; the Source seam does not exist in this port (stdlib bytes/BytesIO is the contract), so there is no read(byteCount) primitive to specify. |
| IO-3 | MUST | n/a | Negative-byteCount argument validation on Okio Source.read/Sink.write/Buffer.copyTo; those size-taking seam primitives were removed with the byte-stream provider seam (docs/archite |
| IO-4 | MUST | n/a | Okio Sink.write head-removal/short-write contract; the Sink seam was removed in favour of RequestBody.iter_bytes over stdlib bytes. |
| IO-5 | MUST | n/a | Okio Sink.flush() plus closeable Source/Sink; the Source/Sink seam was removed (bodies stream via iter_bytes/aiter_bytes, not a flushable Sink). |
| IO-6 | MUST | n/a | Okio provider wrapping/ownership of a caller-supplied underlying stream; the provider seam was removed and stream ownership is handled by the transport adapters, not a buffered Sou |
| IO-7 | MUST | n/a | Okio Buffer FIFO-that-is-both-source-and-sink; the Buffer seam was removed (an in-memory queue is just bytes/BytesIO here). |
| IO-8 | MUST | n/a | Okio Buffer.snapshot() independent-copy contract; the Buffer seam was removed, so there is no Buffer.snapshot to specify (the log-tap snapshot on LoggableRequestBody is a distinct |
| IO-9 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-10 | MUST | n/a | Okio Buffer.clear()/copyTo(window) non-consuming copy; the Buffer seam was removed. |
| IO-11 | MUST | n/a | Okio BufferedSource exhausted()/readByte()/readByteArray(); the BufferedSource seam was removed (drain via iter_bytes/bytes() over stdlib bytes). |
| IO-12 | MUST | n/a | Okio exact-count read (readByteArray(n)/readUtf8(n)) EOF-on-short contract; the BufferedSource seam was removed. |
| IO-13 | MUST | n/a | Okio UTF-8/explicit-charset read/write surface (readUtf8/writeUtf8/writeString(charset)); the BufferedSource/BufferedSink seam was removed, encoding is done with stdlib str.encode/ |
| IO-14 | MUST | n/a | Okio BufferedSource.readUtf8Line() terminator semantics; the BufferedSource seam was removed (SSE/streaming line handling is a separate subsystem, not this Okio primitive). |
| IO-15 | MUST | n/a | Okio BufferedSource.skip(byteCount) EOF contract; the BufferedSource seam was removed. |
| IO-16 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-17 | MUST | n/a | Okio Sink.writeAll(source) pump-to-exhaustion/return-count contract; the Source/Sink seam was removed. |
| IO-18 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-19 | MUST | n/a | Okio BufferedSource.peek() non-consuming view; the BufferedSource seam was removed (repeatable reads are achieved by LoggableResponseBody caching, not a peek view). |
| IO-20 | MUST | n/a | Okio BufferedSource.slice(offset, byteCount) non-consuming length-bounded view; the slice/BufferedSource seam was removed. |
| IO-21 | MUST | n/a | Okio slice lazy offset-overflow vs eager negative-arg rejection; the slice seam was removed. |
| IO-22 | MUST | n/a | Okio slice/parent close-invalidation contract; the slice seam was removed. |
| IO-23 | MUST | n/a | Okio multiple-slice/peek mutual independence and slice-of-slice offset composition; the slice/peek seam was removed. |
| IO-24 | MUST | n/a | Okio read-after-explicit-slice-close state-error contract; the slice seam was removed. |
| IO-25 | MUST | test | 1 test(s) |
| IO-26 | MUST | test | 1 test(s) |
| IO-27 | MUST | test | 2 test(s) |
| IO-28 | MUST | n/a | Okio TeeSink's guard against exposing its backing Buffer accessor; the Sink seam was removed and the LoggableRequestBody decorator exposes only iter_bytes/snapshot/captured_size (n |
| IO-29 | MUST | n/a | Okio TeeSink's flush()/close()/emit() forwarding-to-primary-only lifecycle; flush/emit are Sink methods that do not exist on the RequestBody/ResponseBody surface (the Sink lifecycl |
| IO-30 | MUST | n/a | Okio provider factory operations (create Buffer, wrap streams/byte-array as BufferedSource/Sink); the provider seam was removed, bodies are constructed via RequestBody/ResponseBody |
| IO-31 | MUST | n/a | Okio provider-registry resolution precedence via classpath auto-discovery; Protocols replace Java interfaces and there is no ServiceLoader auto-discovery idiom in this port, so no |
| IO-32 | MUST | n/a | Okio provider install idempotency/conflict-rejection; the provider registry seam was removed (no ServiceLoader-style installable provider). |
| IO-33 | MUST | n/a | Okio explicit-install-wins-over-auto-discovery precedence; the provider registry and auto-discovery seam were removed. |
| IO-34 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-35 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-36 | MAY | uncovered | MAY-level, non-gating |
| IO-37 | MUST | n/a | Single-threaded-contract declaration for Okio Source/Sink/BufferedSource/BufferedSink/Buffer/TeeSink; those streaming instance types were removed with the byte-stream seam. |
| IO-38 | MUST | n/a | Cross-thread observability of a source/buffer close-state to derived slices; the slice/Buffer seam was removed, so there are no derived slices to invalidate. |
| IO-39 | SHOULD | uncovered | SHOULD-level, non-gating |
| IO-40 | MUST | n/a | Okio no-op Timeout on wrapped streams plus non-swallowing/non-duplicating cancellation forwarding by a mirroring Sink; the Okio Timeout and Source/Sink seam were removed (in-memory |
| IO-41 | MUST | n/a | Idempotent close() / close-at-most-once for Okio Source/Sink/Buffer; those seam types were removed (close-idempotency of bodies, responses, SSE connections and pipelines is exercis |
| IO-42 | MUST | n/a | Okio buffered-source/sink use-after-close rejection with in-memory-buffer exemption and slice invalidation; the BufferedSource/BufferedSink/slice seam was removed (LoggableResponse |

## nfr

| ID | Level | Status | Evidence |
|---|---|---|---|
| NFR-1 | MUST | test | 4 test(s) |
| NFR-2 | SHOULD | test | 3 test(s) |
| NFR-3 | SHOULD | test | 9 test(s) |
| NFR-4 | SHOULD | test | 3 test(s) |
| NFR-5 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-6 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-7 | SHOULD | test | 3 test(s) |
| NFR-8 | MUST | n/a | The requirement's own escape clause ("In ecosystems without such a build step this requirement does not apply") applies: CPython wheels have no whole-program dead-code elimination/ |
| NFR-9 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-10 | MUST | deviation | The lowest-supported-runtime floor is declared as requires-python>=3.12 in every published pyproject and enforced by the CI 3.12/3.13/3.14 matrix running the full suite on the floo |
| NFR-11 | SHOULD | test | 2 test(s) |
| NFR-12 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-13 | SHOULD | test | 3 test(s) |
| NFR-14 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-15 | SHOULD | test | 3 test(s) |
| NFR-16 | SHOULD | uncovered | SHOULD-level, non-gating |
| NFR-17 | MUST | deviation | The quality gates are wired blocking (fail the run) in .github/workflows/ci.yml and release.yml — pytest, traceability audit (DEXPACE_TRACEABILITY_GATE), mypy --strict, ruff/ruff-f |

## observability

| ID | Level | Status | Evidence |
|---|---|---|---|
| OBS-1 | MUST | test | 2 test(s) |
| OBS-2 | MUST | test | 2 test(s) |
| OBS-3 | MUST | test | 2 test(s) |
| OBS-4 | MUST | test | 2 test(s) |
| OBS-5 | MUST | test | 1 test(s) |
| OBS-6 | MUST | test | 2 test(s) |
| OBS-7 | SHOULD | test | 2 test(s) |
| OBS-8 | MUST | test | 1 test(s) |
| OBS-9 | MUST | test | 1 test(s) |
| OBS-10 | MUST | test | 3 test(s) |
| OBS-11 | MUST | test | 2 test(s) |
| OBS-12 | MUST | test | 3 test(s) |
| OBS-13 | MUST | test | 2 test(s) |
| OBS-14 | MUST | test | 3 test(s) |
| OBS-15 | MUST | test | 2 test(s) |
| OBS-16 | MUST | test | 6 test(s) |
| OBS-17 | MUST | test | 6 test(s) |
| OBS-18 | MUST | test | 5 test(s) |
| OBS-19 | SHOULD | uncovered | SHOULD-level, non-gating |
| OBS-20 | MUST | test | 3 test(s) |
| OBS-21 | MUST | test | 4 test(s) |
| OBS-22 | MUST | test | 4 test(s) |
| OBS-23 | MUST | test | 3 test(s) |
| OBS-24 | MUST | test | 2 test(s) |
| OBS-25 | MUST | test | 3 test(s) |
| OBS-26 | MUST | test | 3 test(s) |
| OBS-27 | MUST | test | 4 test(s) |
| OBS-28 | SHOULD | test | 2 test(s) |
| OBS-29 | MUST | test | 3 test(s) |
| OBS-30 | MUST | test | 3 test(s) |
| OBS-31 | MUST | test | 3 test(s) |
| OBS-32 | SHOULD | uncovered | SHOULD-level, non-gating |
| OBS-33 | MUST | test | 4 test(s) |
| OBS-34 | MUST | test | 5 test(s) |
| OBS-35 | SHOULD | uncovered | SHOULD-level, non-gating |
| OBS-36 | MUST | test | 3 test(s) |
| OBS-37 | SHOULD | test | 1 test(s) |
| OBS-38 | SHOULD | test | 2 test(s) |
| OBS-39 | MUST | test | 4 test(s) |
| OBS-40 | SHOULD | test | 1 test(s) |

## pagination

| ID | Level | Status | Evidence |
|---|---|---|---|
| PAGE-1 | MUST | test | 2 test(s) |
| PAGE-2 | MUST | test | 1 test(s) |
| PAGE-3 | MUST | test | 2 test(s) |
| PAGE-4 | MUST | test | 3 test(s) |
| PAGE-5 | MUST | test | 1 test(s) |
| PAGE-6 | MUST | test | 3 test(s) |
| PAGE-7 | MUST | test | 2 test(s) |
| PAGE-8 | MUST | test | 2 test(s) |
| PAGE-9 | MUST | test | 4 test(s) |
| PAGE-10 | SHOULD | test | 1 test(s) |
| PAGE-11 | MUST | test | 1 test(s) |
| PAGE-12 | MUST | test | 2 test(s) |
| PAGE-13 | MUST | test | 1 test(s) |
| PAGE-14 | MUST | test | 1 test(s) |
| PAGE-15 | MUST | test | 2 test(s) |
| PAGE-16 | MUST | test | 3 test(s) |
| PAGE-17 | MUST | test | 4 test(s) |
| PAGE-18 | MUST | test | 10 test(s) |
| PAGE-19 | MUST | test | 3 test(s) |
| PAGE-20 | SHOULD | test | 2 test(s) |
| PAGE-21 | MUST | test | 17 test(s) |
| PAGE-22 | MUST | test | 5 test(s) |
| PAGE-23 | MUST | deviation | Replace-first-in-place, append-when-absent, remove-on-null, and follow-whole-URL-preserving-method/headers are all covered by the cited tests; BUT the MUST sub-clause 'dropping any |
| PAGE-24 | MUST | test | 8 test(s) |
| PAGE-25 | MUST | test | 1 test(s) |
| PAGE-26 | MUST | test | 1 test(s) |
| PAGE-27 | MUST | test | 3 test(s) |
| PAGE-28 | MUST | test | 3 test(s) |
| PAGE-29 | MUST | test | 2 test(s) |
| PAGE-30 | MUST | n/a | Genuinely not applicable: AsyncPaginator accepts no caller-supplied driver executor (constructor: source, strategy, initial_request, max_pages, dispatch_factory), so the entire req |
| PAGE-31 | SHOULD | test | 1 test(s) |
| PAGE-32 | MUST | test | 2 test(s) |
| PAGE-33 | MUST | test | 2 test(s) |
| PAGE-34 | MUST | test | 2 test(s) |
| PAGE-35 | SHOULD | test | 1 test(s) |
| PAGE-36 | MUST | test | 2 test(s) |

## pipeline

| ID | Level | Status | Evidence |
|---|---|---|---|
| PIPE-1 | MUST | test | 3 test(s) |
| PIPE-2 | MUST | test | 9 test(s) |
| PIPE-3 | SHOULD | test | 16 test(s) |
| PIPE-4 | MUST | test | 8 test(s) |
| PIPE-5 | MUST | test | 3 test(s) |
| PIPE-6 | MUST | test | 4 test(s) |
| PIPE-7 | MUST | test | 3 test(s) |
| PIPE-8 | MUST | test | 2 test(s) |
| PIPE-9 | MUST | test | 2 test(s) |
| PIPE-10 | MUST | test | 2 test(s) |
| PIPE-11 | MUST | test | 1 test(s) |
| PIPE-12 | MUST | test | 3 test(s) |
| PIPE-13 | MUST | test | 3 test(s) |
| PIPE-14 | MUST | test | 1 test(s) |
| PIPE-15 | MUST | test | 1 test(s) |
| PIPE-16 | MUST | test | 2 test(s) |
| PIPE-17 | MUST | test | 2 test(s) |
| PIPE-18 | MUST | test | 3 test(s) |
| PIPE-19 | MUST | test | 2 test(s) |
| PIPE-20 | MUST | test | 1 test(s) |
| PIPE-21 | MUST | test | 1 test(s) |
| PIPE-22 | MUST | test | 2 test(s) |
| PIPE-23 | MUST | test | 2 test(s) |
| PIPE-24 | MUST | test | 2 test(s) |
| PIPE-25 | MUST | test | 2 test(s) |
| PIPE-26 | MUST | deviation | The Python pipeline exposes run(request, dispatch, **options) and is consumed via the structural SyncPipelineLike/AsyncPipelineLike Protocols (pipeline/dispatch.py) rather than lit |
| PIPE-27 | MUST | deviation | Rather than making pipeline close a strict no-op, Pipeline.__exit__ delegates to transport.close() (the CLAUDE.md context-manager convention) and transport adapters are ownership-a |
| PIPE-28 | MUST | test | 3 test(s) |
| PIPE-29 | MUST | test | 2 test(s) |
| PIPE-30 | MUST | test | 2 test(s) |
| PIPE-31 | MUST | test | 3 test(s) |
| PIPE-32 | MUST | test | 2 test(s) |
| PIPE-33 | MUST | deviation | The sync->async bridge lives at the transport seam (client/_sync_to_async.py SyncToAsyncHttpClient) and mandates a caller-supplied executor with no default (tests test_missing_exec |
| PIPE-34 | MUST | deviation | The async->sync bridge lives at the transport seam (client/_async_to_sync.py AsyncToSyncHttpClient): it blocks the calling thread for each call and on KeyboardInterrupt cancels the |
| PIPE-35 | SHOULD | uncovered | SHOULD-level, non-gating |
| PIPE-36 | SHOULD | test | 2 test(s) |
| PIPE-37 | MUST | test | 2 test(s) |
| PIPE-38 | MUST | test | 6 test(s) |
| PIPE-39 | SHOULD | test | 2 test(s) |
| PIPE-40 | MUST | test | 3 test(s) |

## recovery

| ID | Level | Status | Evidence |
|---|---|---|---|
| RECOV-1 | MUST | test | 2 test(s) |
| RECOV-2 | MUST | test | 2 test(s) |
| RECOV-3 | MUST | deviation | The port has no dedicated RequestRecoveryChain container; ordered request-side transforms, empty-set identity, and short-circuit-and-propagate-on-throw are realized by the pipeline |
| RECOV-4 | MUST | test | 2 test(s) |
| RECOV-5 | MUST | test | 1 test(s) |
| RECOV-6 | MUST | deviation | There is no chain container that iterates a response-step list then a recovery-step list; the response-then-recovery ordering emerges from composing map_success (Success-path) befo |
| RECOV-7 | MUST | test | 1 test(s) |
| RECOV-8 | MUST | test | 1 test(s) |
| RECOV-9 | SHOULD | test | 2 test(s) |
| RECOV-10 | MUST | test | 2 test(s) |
| RECOV-11 | MUST | test | 2 test(s) |
| RECOV-12 | MUST | test | 2 test(s) |
| RECOV-13 | MUST | test | 2 test(s) |
| RECOV-14 | MUST | deviation | No chain container defensively copies step lists; instead each policy defensively frozenset-copies its own collections (RetryPolicy.method_allowlist/retry_on_status_codes are froze |
| RECOV-15 | MUST | test | 3 test(s) |
| RECOV-16 | MUST | test | 2 test(s) |
| RECOV-17 | MUST | test | 3 test(s) |
| RECOV-18 | MUST | test | 2 test(s) |
| RECOV-19 | MUST | test | 3 test(s) |
| RECOV-20 | MUST | deviation | Retries are bounded by a max-attempts cap (initial send counts as attempt 1; asserted by test_sync_max_retries_two_sends_exactly_three_times) AND a per-call total-timeout deadline |
| RECOV-21 | MUST | deviation | The exponential curve base*multiplier^(n-1) capped at maxDelay is ported faithfully and tested, but the jitter is a different scheme — AWS full-jitter [0.5,1.0] by default and a sy |
| RECOV-22 | MUST | test | 2 test(s) |
| RECOV-23 | MUST | test | 3 test(s) |
| RECOV-24 | MUST | test | 17 test(s) |
| RECOV-25 | SHOULD | test | 2 test(s) |
| RECOV-26 | MUST | test | 2 test(s) |
| RECOV-27 | MUST | test | 3 test(s) |
| RECOV-28 | MUST | test | 2 test(s) |
| RECOV-29 | MUST | test | 2 test(s) |
| RECOV-30 | SHOULD | test | 1 test(s) |
| RECOV-31 | MAY | uncovered | MAY-level, non-gating |
| RECOV-32 | MUST | test | 6 test(s) |
| RECOV-33 | MUST | test | 3 test(s) |
| RECOV-34 | MUST | test | 21 test(s) |

## redirect

| ID | Level | Status | Evidence |
|---|---|---|---|
| REDIR-1 | MUST | test | 3 test(s) |
| REDIR-2 | MUST | test | 1 test(s) |
| REDIR-3 | MUST | test | 3 test(s) |
| REDIR-4 | MUST | test | 2 test(s) |
| REDIR-5 | MUST | deviation | Verified: the 303 GET-rebuild mechanics match and are tested (test_303_follow_303_true_reissues_as_get_and_drops_body: GET, body dropped, Content-* removed; test_303_follow_303_fal |
| REDIR-6 | MUST | test | 2 test(s) |
| REDIR-7 | MUST | test | 6 test(s) |
| REDIR-8 | MUST | test | 4 test(s) |
| REDIR-9 | MUST | test | 6 test(s) |
| REDIR-10 | SHOULD | uncovered | SHOULD-level, non-gating |
| REDIR-11 | MUST | deviation | Verified: the port plants no out-of-band wire marker. Instead each credential policy records the seed origin via ctx.data.setdefault(_AUTH_ORIGIN_KEY, current) on first pass and re |
| REDIR-12 | MUST | test | 15 test(s) |
| REDIR-13 | MUST | test | 13 test(s) |
| REDIR-14 | MUST | test | 1 test(s) |
| REDIR-15 | MUST | test | 6 test(s) |
| REDIR-16 | MUST | test | 2 test(s) |
| REDIR-17 | MUST | test | 2 test(s) |
| REDIR-18 | MUST | test | 1 test(s) |
| REDIR-19 | MUST | test | 2 test(s) |
| REDIR-20 | MUST | test | 4 test(s) |
| REDIR-21 | SHOULD | test | 1 test(s) |
| REDIR-22 | MUST | test | 3 test(s) |
| REDIR-23 | SHOULD | uncovered | SHOULD-level, non-gating |
| REDIR-24 | MUST | test | 2 test(s) |
| REDIR-25 | MUST | test | 2 test(s) |
| REDIR-26 | MUST | deviation | Verified: the port satisfies the defensive-copy requirement natively via type immutability rather than a copy — allowed_methods is typed and stored as a frozenset (redirect.py __in |
| REDIR-27 | MAY | uncovered | MAY-level, non-gating |
| REDIR-28 | SHOULD | uncovered | SHOULD-level, non-gating |

## retry

| ID | Level | Status | Evidence |
|---|---|---|---|
| RETRY-1 | MUST | deviation | The classifier IS single-sourced (DEFAULT_RETRYABLE_STATUS in pipeline/_pure/classifier.py, consumed by both HttpResponseError.retryable and RetryPolicy's default) and that single- |
| RETRY-2 | MUST | test | 3 test(s) |
| RETRY-3 | MUST | test | 7 test(s) |
| RETRY-4 | MUST | test | 3 test(s) |
| RETRY-5 | MUST | test | 3 test(s) |
| RETRY-6 | MUST | deviation | CORRECTED from marker. The idempotent-method set IS single-sourced (_DEFAULT_METHOD_ALLOWLIST in retry.py:100) and POST/PATCH are correctly excluded (retried only on 500/503/504 vi |
| RETRY-7 | MUST | test | 2 test(s) |
| RETRY-8 | MUST | test | 2 test(s) |
| RETRY-9 | MUST | deviation | compute_backoff implements base*multiplier**(attempt-1) clamped to cap (tested: test_exponential_growth, test_cap_saturates), BUT it special-cases attempt<=1 to return 0.0 (retry.p |
| RETRY-10 | MUST | test | 2 test(s) |
| RETRY-11 | MUST | test | 2 test(s) |
| RETRY-12 | SHOULD | uncovered | SHOULD-level, non-gating |
| RETRY-13 | MUST | test | 1 test(s) |
| RETRY-14 | MUST | test | 2 test(s) |
| RETRY-15 | MUST | test | 6 test(s) |
| RETRY-16 | MUST | test | 2 test(s) |
| RETRY-17 | MUST | test | 2 test(s) |
| RETRY-18 | MUST | test | 2 test(s) |
| RETRY-19 | MUST | test | 28 test(s) |
| RETRY-20 | MUST | test | 2 test(s) |
| RETRY-21 | MUST | test | 2 test(s) |
| RETRY-22 | MUST | test | 2 test(s) |
| RETRY-23 | MUST | test | 2 test(s) |
| RETRY-24 | MUST | test | 2 test(s) |
| RETRY-25 | MUST | test | 3 test(s) |
| RETRY-26 | MUST | test | 2 test(s) |
| RETRY-27 | MUST | test | 2 test(s) |
| RETRY-28 | MUST | deviation | This unified port models its single RetryPolicy on Azure corehttp, whose 'timeout' knob is an always-on total budget with a large default (604800s / 7 days, documented inline as 'mirroring Azure's def… |
| RETRY-29 | MAY | uncovered | MAY-level, non-gating |
| RETRY-30 | MUST | test | 2 test(s) |
| RETRY-31 | MUST | test | 3 test(s) |
| RETRY-32 | MUST | test | 2 test(s) |
| RETRY-33 | MUST | test | 2 test(s) |
| RETRY-34 | MUST | test | 3 test(s) |
| RETRY-35 | MUST | test | 2 test(s) |
| RETRY-36 | MUST | test | 2 test(s) |
| RETRY-37 | MUST | test | 3 test(s) |
| RETRY-38 | SHOULD | uncovered | SHOULD-level, non-gating |
| RETRY-39 | MUST | test | 2 test(s) |
| RETRY-40 | SHOULD | uncovered | SHOULD-level, non-gating |
| RETRY-41 | MUST | test | 2 test(s) |
| RETRY-42 | MUST | test | 2 test(s) |
| RETRY-43 | MAY | test | 1 test(s) |
| RETRY-44 | MUST | test | 2 test(s) |
| RETRY-45 | MUST | n/a | Scheduler-lifecycle ownership (never shut down a caller-supplied scheduler; a process-wide daemon-thread scheduler VM-wide; the async stage stack requiring a caller-supplied schedu |

## seams

| ID | Level | Status | Evidence |
|---|---|---|---|
| SEAM-1 | MUST | test | 3 test(s) |
| SEAM-2 | MUST | test | 2 test(s) |
| SEAM-3 | MUST | n/a | the byte-stream provider / Io / IoProvider / Okio-style seam was deliberately removed (confirmed absent in core source) — CLAUDE.md states Python stdlib bytes/bytearray/memoryview/ |
| SEAM-4 | MUST | n/a | no byte-stream provider factory exists to be thread-safe — the whole provider seam was removed in favour of the stdlib bytes/BytesIO/BinaryIO contract (CLAUDE.md removed-Io-seam de |
| SEAM-5 | MUST | n/a | provider resolution/precedence has no port: the byte-stream provider seam is removed, and there is no classpath ServiceLoader auto-discovery idiom on CPython — Protocols replace Ja |
| SEAM-6 | MUST | n/a | explicit provider installation/idempotency has no port — the byte-stream provider seam it governs was removed (CLAUDE.md removed-Io-seam deviation); no install registry exists in c |
| SEAM-7 | MUST | n/a | process-wide caching of a discovered provider has no port — there is no provider seam and no classpath discovery scan to cache (removed-Io-seam + no-ServiceLoader deviations). |
| SEAM-8 | SHOULD | uncovered | SHOULD-level, non-gating |
| SEAM-9 | MUST | n/a | provider resolve/install/swap concurrency-safety has no port — the provider registry it protects was removed; the stdlib byte types carry no install state (removed-Io-seam deviatio |
| SEAM-10 | SHOULD | uncovered | SHOULD-level, non-gating |
| SEAM-11 | MUST | test | 8 test(s) |
| SEAM-12 | MUST | test | 12 test(s) |
| SEAM-13 | SHOULD | test | 5 test(s) |
| SEAM-14 | MUST | test | 6 test(s) |
| SEAM-15 | MAY | test | 8 test(s) |
| SEAM-16 | MUST | test | 5 test(s) |
| SEAM-17 | SHOULD | test | 5 test(s) |
| SEAM-18 | MUST | test | 3 test(s) |
| SEAM-19 | MUST | test | 2 test(s) |
| SEAM-20 | MUST | test | 4 test(s) |
| SEAM-21 | MUST | test | 3 test(s) |
| SEAM-22 | MUST | test | 2 test(s) |
| SEAM-23 | MUST | test | 3 test(s) |
| SEAM-24 | SHOULD | test | 1 test(s) |
| SEAM-25 | MUST | test | 2 test(s) |
| SEAM-26 | MUST | test | 2 test(s) |
| SEAM-27 | MUST | test | 5 test(s) |
| SEAM-28 | MAY | test | 2 test(s) |
| SEAM-29 | MUST | n/a | the immutable-value + Builder pattern was deliberately not ported: CLAUDE.md mandates frozen @dataclass(slots=True) models constructed via keyword/default arguments ('Builders are |
| SEAM-30 | MUST | test | 5 test(s) |

## serde

| ID | Level | Status | Evidence |
|---|---|---|---|
| SERDE-1 | MUST | test | 2 test(s) |
| SERDE-2 | MUST | test | 2 test(s) |
| SERDE-3 | MUST | test | 3 test(s) |
| SERDE-4 | MUST | test | 4 test(s) |
| SERDE-5 | MUST | test | 2 test(s) |
| SERDE-6 | MUST | test | 3 test(s) |
| SERDE-7 | MUST | test | 2 test(s) |
| SERDE-8 | MUST | test | 3 test(s) |
| SERDE-9 | MUST | test | 3 test(s) |
| SERDE-10 | MUST | test | 3 test(s) |
| SERDE-11 | SHOULD | test | 1 test(s) |
| SERDE-12 | MUST | test | 3 test(s) |
| SERDE-13 | MUST | test | 8 test(s) |
| SERDE-14 | MUST | test | 3 test(s) |
| SERDE-15 | MUST | test | 3 test(s) |
| SERDE-16 | MUST | test | 3 test(s) |
| SERDE-17 | MUST | test | 3 test(s) |
| SERDE-18 | SHOULD | test | 7 test(s) |
| SERDE-19 | MUST | test | 4 test(s) |
| SERDE-20 | SHOULD | test | 1 test(s) |
| SERDE-21 | MUST | deviation | The codec is deliberately validation-free (codec.py module docstring: 'performs no schema checks or scalar coercion'); cross-shape scalars pass through unchanged ('5'->str '5', 1.5 |
| SERDE-22 | MUST | test | 9 test(s) |
| SERDE-23 | SHOULD | test | 2 test(s) |
| SERDE-24 | SHOULD | test | 3 test(s) |
| SERDE-25 | SHOULD | test | 2 test(s) |
| SERDE-26 | MUST | test | 1 test(s) |
| SERDE-27 | MUST | deviation | The response-decode lifecycle/error contract is fully implemented and tested (closes the response on every path, surfaces a missing body as a DeserializationError naming the target |
| SERDE-28 | MUST | test | 7 test(s) |
| SERDE-29 | SHOULD | test | 1 test(s) |
| SERDE-30 | MAY | test | 1 test(s) |

## sse

| ID | Level | Status | Evidence |
|---|---|---|---|
| SSE-1 | MUST | test | 2 test(s) |
| SSE-2 | MUST | test | 4 test(s) |
| SSE-3 | MUST | test | 2 test(s) |
| SSE-4 | MUST | test | 2 test(s) |
| SSE-5 | MUST | test | 13 test(s) |
| SSE-6 | MUST | test | 3 test(s) |
| SSE-7 | MUST | test | 1 test(s) |
| SSE-8 | MUST | deviation | Confirmed: _data_lines accumulates consecutive data lines in wire order but they are joined with a single '\n' into an immutable SseEvent.data string at dispatch rather than expose |
| SSE-9 | MUST | test | 3 test(s) |
| SSE-10 | MUST | test | 2 test(s) |
| SSE-11 | MUST | test | 4 test(s) |
| SSE-12 | MUST | test | 3 test(s) |
| SSE-13 | MUST | test | 7 test(s) |
| SSE-14 | MUST | test | 3 test(s) |
| SSE-15 | MUST | test | 2 test(s) |
| SSE-16 | MUST | test | 1 test(s) |
| SSE-17 | MUST | test | 1 test(s) |
| SSE-18 | MUST | test | 1 test(s) |
| SSE-19 | MAY | uncovered | MAY-level, non-gating |
| SSE-20 | MUST | deviation | Confirmed: SseEvent is a frozen(slots) dataclass (immutable) whose payload is an immutable joined string rather than a defensively-copied data list (consequence of SSE-8), so there |
| SSE-21 | SHOULD | test | 2 test(s) |
| SSE-22 | SHOULD | uncovered | SHOULD-level, non-gating |
| SSE-23 | MUST | test | 3 test(s) |
| SSE-24 | MUST | test | 2 test(s) |
| SSE-25 | MUST | test | 3 test(s) |
| SSE-26 | MUST | test | 2 test(s) |
| SSE-27 | MUST | test | 3 test(s) |
| SSE-28 | MUST | test | 3 test(s) |
| SSE-29 | MUST | test | 3 test(s) |
| SSE-30 | MUST | test | 6 test(s) |
| SSE-31 | MUST | test | 1 test(s) |
| SSE-32 | MUST | test | 2 test(s) |
| SSE-33 | MUST | test | 2 test(s) |
| SSE-34 | MUST | test | 3 test(s) |
| SSE-35 | MUST | test | 3 test(s) |
| SSE-36 | MUST | test | 2 test(s) |
| SSE-37 | MUST | test | 1 test(s) |
| SSE-38 | MUST | deviation | The core parser conforms — it surfaces the server retry hint and each event's raw per-block id, no longer persists a last-event-id across events (SSE-16), and sets no request header. Reconnection and … |
| SSE-39 | MUST | test | 2 test(s) |
| SSE-40 | SHOULD | test | 2 test(s) |
| SSE-41 | MAY | test | 1 test(s) |

## transport

| ID | Level | Status | Evidence |
|---|---|---|---|
| TRANSPORT-1 | MUST | test | 11 test(s) |
| TRANSPORT-2 | MUST | test | 10 test(s) |
| TRANSPORT-3 | MUST | test | 9 test(s) |
| TRANSPORT-4 | MUST | deviation | The cancel-flag clause is honored and tested (read timeout maps to ServiceResponseTimeoutError, never RequestCancelledError, classified as timeout), but the port deliberately devia |
| TRANSPORT-5 | MUST | test | 6 test(s) |
| TRANSPORT-6 | SHOULD | test | 4 test(s) |
| TRANSPORT-7 | MUST | test | 6 test(s) |
| TRANSPORT-8 | MUST | n/a | OkHttp's interceptor-driven internal cancel-all is a library-specific seam with no analog in httpx/aiohttp/requests/urllib (like java.net.http, which the requirement text itself ca |
| TRANSPORT-9 | MUST | n/a | The adaptation race is an artifact of OkHttp's Call.enqueue + CompletableFuture callback dispatch where a native response can arrive after the future already completed/cancelled; a |
| TRANSPORT-10 | MUST | test | 9 test(s) |
| TRANSPORT-11 | MUST | test | 6 test(s) |
| TRANSPORT-12 | MUST | test | 8 test(s) |
| TRANSPORT-13 | SHOULD | test | 8 test(s) |
| TRANSPORT-14 | MUST | test | 6 test(s) |
| TRANSPORT-15 | MUST | test | 7 test(s) |
| TRANSPORT-16 | MUST | test | 3 test(s) |
| TRANSPORT-17 | MUST | test | 12 test(s) |
| TRANSPORT-18 | MUST | test | 8 test(s) |
| TRANSPORT-19 | SHOULD | uncovered | SHOULD-level, non-gating |
| TRANSPORT-20 | MUST | test | 12 test(s) |
| TRANSPORT-21 | MUST | test | 4 test(s) |
| TRANSPORT-22 | MUST | test | 8 test(s) |
| TRANSPORT-23 | MUST | test | 8 test(s) |
| TRANSPORT-24 | MUST | test | 14 test(s) |
| TRANSPORT-25 | MUST | test | 12 test(s) |
| TRANSPORT-26 | MUST | test | 10 test(s) |
| TRANSPORT-27 | SHOULD | test | 8 test(s) |
| TRANSPORT-28 | SHOULD | test | 5 test(s) |
| TRANSPORT-29 | MUST | test | 12 test(s) |
| TRANSPORT-30 | SHOULD | test | 8 test(s) |

## xcut

| ID | Level | Status | Evidence |
|---|---|---|---|
| XCUT-1 | MUST | test | 2 test(s) |
| XCUT-2 | MUST | test | 3 test(s) |
| XCUT-3 | MUST | test | 3 test(s) |
| XCUT-4 | MUST | test | 4 test(s) |
| XCUT-5 | MUST | test | 119 test(s) |
| XCUT-6 | MUST | test | 5 test(s) |
| XCUT-7 | MUST | test | 3 test(s) |
| XCUT-8 | MUST | test | 13 test(s) |
| XCUT-9 | MUST | test | 3 test(s) |
| XCUT-10 | MUST | test | 5 test(s) |
| XCUT-11 | MUST | test | 3 test(s) |
| XCUT-12 | SHOULD | test | 3 test(s) |
| XCUT-13 | MUST | test | 2 test(s) |
| XCUT-14 | MUST | test | 3 test(s) |
| XCUT-15 | MUST | test | 3 test(s) |
| XCUT-16 | MUST | test | 3 test(s) |
| XCUT-17 | MUST | test | 19 test(s) |
| XCUT-18 | MUST | test | 10 test(s) |
| XCUT-19 | MUST | test | 9 test(s) |
| XCUT-20 | MUST | test | 4 test(s) |
| XCUT-21 | MUST | test | 3 test(s) |
| XCUT-22 | MUST | test | 3 test(s) |
| XCUT-23 | MUST | n/a | This requirement is the pluggable I/O-provider SPI with classpath/ServiceLoader auto-discovery and zero/multiple-candidate resolution; that byte-stream provider (Io/IoProvider/Okio |
| XCUT-24 | SHOULD | test | 3 test(s) |
