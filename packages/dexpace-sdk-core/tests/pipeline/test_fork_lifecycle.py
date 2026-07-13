# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Fork / re-drive and response-lifecycle battery for both pipeline drivers.

Pins the pipeline runtime's per-``send`` semantics on the sync ``Pipeline`` and
the async ``AsyncPipeline``:

- **Independent forks.** Each ``self.next.send`` re-drive (retry / redirect
  re-issuing a request) re-runs the whole downstream chain; a substituted
  request sticks downstream for that fork; per-call ``options`` ride unchanged
  across every fork.
- **Supersede close ordering.** A policy that supersedes a response closes the
  superseded response *before* re-driving and hands the final response back
  *open*.
- **Abandon paths stay open.** Redirect loop / max-hops and an exhausted retry
  budget return the in-flight response open, never closed.
- **Transport ownership.** The ``send`` / fork machinery never closes the
  transport; only the deliberate context-manager exit does.
- **Concurrency.** 100 parallel sends through one built pipeline never corrupt
  each other's per-call state.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, ResponseBody, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline, PipelineContext, Policy, Stage
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy
from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy

from ..conftest import FakeClock

# --------------------------------------------------------------------------- #
# Shared fixtures / factories                                                  #
# --------------------------------------------------------------------------- #

#: One scripted hop: the status to return and an optional ``Location`` header.
type _Hop = tuple[Status, str | None]


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _dispatch(trace: str) -> DispatchContext:
    """Build a fresh ``DispatchContext`` for a unique 32-hex-char trace id."""
    return DispatchContext(_instr(trace))


def _get(url: str = "https://example.com/start") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _AsyncFakeClock:
    """Deterministic async clock; ``sleep`` advances simulated time."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)


# --------------------------------------------------------------------------- #
# Close-counting response bodies                                              #
# --------------------------------------------------------------------------- #


class _CountingBody(ResponseBody):
    """Sync response body that counts how many times ``close`` was called."""

    __slots__ = ("close_count",)

    def __init__(self) -> None:
        self.close_count = 0

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield b""

    def close(self) -> None:
        self.close_count += 1

    @property
    def closed(self) -> bool:
        return self.close_count > 0


class _AsyncCountingBody(AsyncResponseBody):
    """Async twin of ``_CountingBody``."""

    __slots__ = ("close_count",)

    def __init__(self) -> None:
        self.close_count = 0

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        yield b""

    async def close(self) -> None:
        self.close_count += 1

    @property
    def closed(self) -> bool:
        return self.close_count > 0


# --------------------------------------------------------------------------- #
# Close-tracking scripted transports                                          #
# --------------------------------------------------------------------------- #


class _ScriptClient(HttpClient):
    """Scripted sync transport; records requests, bodies, and ``close`` calls.

    The transport is the fork counter: every re-drive re-runs the full
    downstream chain and reaches ``execute`` exactly once, so ``len(requests)``
    equals the number of forks. ``close_calls`` proves whether the pipeline
    ever closed its transport.
    """

    def __init__(self, script: Sequence[_Hop]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.bodies: list[_CountingBody] = []
        self.close_calls = 0

    def execute(self, request: Request) -> Response:
        status, location = self._script[len(self.requests)]
        self.requests.append(request)
        body = _CountingBody()
        self.bodies.append(body)
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=status, body=body)
        if location is not None:
            response = response.with_header("Location", location)
        return response

    def close(self) -> None:
        self.close_calls += 1


class _AsyncScriptClient(AsyncHttpClient):
    """Async twin of ``_ScriptClient`` (records ``aclose`` calls)."""

    def __init__(self, script: Sequence[_Hop]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.bodies: list[_AsyncCountingBody] = []
        self.aclose_calls = 0

    async def execute(self, request: Request) -> AsyncResponse:
        status, location = self._script[len(self.requests)]
        self.requests.append(request)
        body = _AsyncCountingBody()
        self.bodies.append(body)
        response = AsyncResponse(
            request=request, protocol=Protocol.HTTP_1_1, status=status, body=body
        )
        if location is not None:
            response = response.with_header("Location", location)
        return response

    async def aclose(self) -> None:
        self.aclose_calls += 1


# --------------------------------------------------------------------------- #
# Probe policies                                                              #
# --------------------------------------------------------------------------- #


class _TeapotRetryPolicy(Policy):
    """Supersedes each 418 response by re-driving the downstream chain.

    On a 418 (I'M A TEAPOT) the probe closes the superseded response, stamps a
    fork counter onto the request, and re-drives ``self.next`` — an independent
    fork whose substituted request sticks downstream. The loop stops at the
    first non-418 response, which is returned open. Per-fork state lives in
    ``ctx.data``, never on ``self``, so the probe is concurrency-safe.
    """

    STAGE = Stage.RETRY
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        fork = 0
        current = request
        while True:
            response = self.next.send(current, ctx)
            if response.status is not Status.IM_A_TEAPOT:
                ctx.data["teapot_forks"] = fork
                return response
            response.close()  # supersede: close before re-driving
            fork += 1
            current = current.with_header("X-Teapot-Fork", str(fork))


class _AsyncTeapotRetryPolicy(AsyncPolicy):
    """Async twin of ``_TeapotRetryPolicy``."""

    STAGE = Stage.RETRY
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        fork = 0
        current = request
        while True:
            response = await self.next.send(current, ctx)
            if response.status is not Status.IM_A_TEAPOT:
                ctx.data["teapot_forks"] = fork
                return response
            await response.close()
            fork += 1
            current = current.with_header("X-Teapot-Fork", str(fork))


class _OptionsProbe(Policy):
    """Records the ``ctx.options`` mapping seen at the transport boundary."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.seen.append(ctx.options)
        return self.next.send(request, ctx)


class _AsyncOptionsProbe(AsyncPolicy):
    """Async twin of ``_OptionsProbe``."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        self.seen.append(ctx.options)
        return await self.next.send(request, ctx)


class _CrossTalkProbe(Policy):
    """Round-trips a per-call id through ``ctx.data`` to detect cross-talk."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        call_id = ctx.options["call_id"]
        ctx.data["seen"] = call_id
        response = self.next.send(request.with_header("X-Call-Id", str(call_id)), ctx)
        # If ``ctx.data`` leaked between concurrent calls, this would echo a
        # different call's id.
        return response.with_header("X-Echo", str(ctx.data["seen"]))


class _AsyncCrossTalkProbe(AsyncPolicy):
    """Async twin of ``_CrossTalkProbe`` with a yield to force interleaving."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        call_id = ctx.options["call_id"]
        ctx.data["seen"] = call_id
        await asyncio.sleep(0)  # cede control so calls interleave
        response = await self.next.send(request.with_header("X-Call-Id", str(call_id)), ctx)
        return response.with_header("X-Echo", str(ctx.data["seen"]))


class _EchoClient(HttpClient):
    """Echoes the request's ``X-Call-Id`` into a response header; counts closes."""

    def __init__(self) -> None:
        self.close_calls = 0

    def execute(self, request: Request) -> Response:
        saw = request.headers.get("X-Call-Id") or ""
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK).with_header(
            "X-Transport-Saw", saw
        )

    def close(self) -> None:
        self.close_calls += 1


class _AsyncEchoClient(AsyncHttpClient):
    """Async twin of ``_EchoClient``."""

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def execute(self, request: Request) -> AsyncResponse:
        saw = request.headers.get("X-Call-Id") or ""
        return AsyncResponse(
            request=request, protocol=Protocol.HTTP_1_1, status=Status.OK
        ).with_header("X-Transport-Saw", saw)

    async def aclose(self) -> None:
        self.aclose_calls += 1


# --------------------------------------------------------------------------- #
# Supersede: closed-exactly-once + final-open, on both drivers                #
# --------------------------------------------------------------------------- #


class TestSupersedeCloseOrdering:
    """A superseding policy closes each superseded response once; final stays open."""

    def test_sync_superseded_closed_once_final_open(self) -> None:
        client = _ScriptClient(
            [(Status.IM_A_TEAPOT, None), (Status.IM_A_TEAPOT, None), (Status.OK, None)]
        )
        with Pipeline(client, policies=[_TeapotRetryPolicy()]) as p:
            response = p.run(_get(), _dispatch("0" * 31 + "1"))
        assert response.status is Status.OK
        # Both superseded 418s were closed exactly once.
        assert [b.close_count for b in client.bodies[:-1]] == [1, 1]
        # The final response is handed back open — the caller owns it.
        assert client.bodies[-1].close_count == 0
        response.close()
        assert client.bodies[-1].close_count == 1

    async def test_async_superseded_closed_once_final_open(self) -> None:
        client = _AsyncScriptClient(
            [(Status.IM_A_TEAPOT, None), (Status.IM_A_TEAPOT, None), (Status.OK, None)]
        )
        async with AsyncPipeline(client, policies=[_AsyncTeapotRetryPolicy()]) as p:
            response = await p.run(_get(), _dispatch("0" * 31 + "2"))
        assert response.status is Status.OK
        assert [b.close_count for b in client.bodies[:-1]] == [1, 1]
        assert client.bodies[-1].close_count == 0
        await response.close()
        assert client.bodies[-1].close_count == 1


# --------------------------------------------------------------------------- #
# Fork counter: full downstream re-run + substituted request sticks           #
# --------------------------------------------------------------------------- #


class TestForkCounter:
    """Each re-drive re-runs the full downstream chain with its substitution."""

    def test_sync_full_rerun_and_substitution_sticks(self) -> None:
        client = _ScriptClient(
            [(Status.IM_A_TEAPOT, None), (Status.IM_A_TEAPOT, None), (Status.OK, None)]
        )
        with Pipeline(client, policies=[_TeapotRetryPolicy()]) as p:
            p.run(_get(), _dispatch("0" * 31 + "3")).close()
        # Three transport hits == original send + two independent forks.
        assert len(client.requests) == 3
        # The fork stamp sticks downstream for that fork and only that fork.
        assert client.requests[0].headers.get("X-Teapot-Fork") is None
        assert client.requests[1].headers.get("X-Teapot-Fork") == "1"
        assert client.requests[2].headers.get("X-Teapot-Fork") == "2"

    async def test_async_full_rerun_and_substitution_sticks(self) -> None:
        client = _AsyncScriptClient(
            [(Status.IM_A_TEAPOT, None), (Status.IM_A_TEAPOT, None), (Status.OK, None)]
        )
        async with AsyncPipeline(client, policies=[_AsyncTeapotRetryPolicy()]) as p:
            response = await p.run(_get(), _dispatch("0" * 31 + "4"))
        await response.close()
        assert len(client.requests) == 3
        assert client.requests[0].headers.get("X-Teapot-Fork") is None
        assert client.requests[1].headers.get("X-Teapot-Fork") == "1"
        assert client.requests[2].headers.get("X-Teapot-Fork") == "2"


# --------------------------------------------------------------------------- #
# Abandon paths return the in-flight response OPEN                            #
# --------------------------------------------------------------------------- #


class TestAbandonPathsReturnOpen:
    """Loop / max-hops / exhausted-budget hand the in-flight response back open."""

    def test_sync_retry_budget_exhausted_returns_open(self) -> None:
        # ``status_retries=1`` exhausts the status sub-budget on the second 503
        # while ``total`` is still positive, forcing the explicit
        # budget-exhausted return branch (not the "no longer retryable" one).
        client = _ScriptClient(
            [(Status.SERVICE_UNAVAILABLE, None), (Status.SERVICE_UNAVAILABLE, None)]
        )
        retry = RetryPolicy(
            total_retries=5, status_retries=1, backoff_factor=0.0, clock=FakeClock()
        )
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), _dispatch("0" * 31 + "5"))
        assert response.status is Status.SERVICE_UNAVAILABLE
        assert client.bodies[0].closed is True  # intermediate closed before sleep
        assert client.bodies[-1].closed is False  # abandoned response returned open
        response.close()

    def test_sync_redirect_loop_returns_open(self) -> None:
        client = _ScriptClient(
            [
                (Status.MOVED_PERMANENTLY, "https://example.com/b"),
                (Status.MOVED_PERMANENTLY, "https://example.com/a"),
            ]
        )
        with Pipeline(client, policies=[RedirectPolicy()]) as p:
            response = p.run(_get("https://example.com/a"), _dispatch("0" * 31 + "6"))
        assert response.status is Status.MOVED_PERMANENTLY
        assert client.bodies[0].closed is True  # first hop superseded and closed
        assert client.bodies[-1].closed is False  # loop-detected response open
        response.close()

    def test_sync_redirect_max_hops_returns_open(self) -> None:
        client = _ScriptClient(
            [
                (Status.MOVED_PERMANENTLY, "https://example.com/a"),
                (Status.MOVED_PERMANENTLY, "https://example.com/b"),
            ]
        )
        with Pipeline(client, policies=[RedirectPolicy(max_hops=1)]) as p:
            response = p.run(_get(), _dispatch("0" * 31 + "7"))
        assert response.status is Status.MOVED_PERMANENTLY
        assert client.bodies[0].closed is True
        assert client.bodies[-1].closed is False
        response.close()

    async def test_async_retry_budget_exhausted_returns_open(self) -> None:
        client = _AsyncScriptClient(
            [(Status.SERVICE_UNAVAILABLE, None), (Status.SERVICE_UNAVAILABLE, None)]
        )
        retry = AsyncRetryPolicy(
            total_retries=5, status_retries=1, backoff_factor=0.0, clock=_AsyncFakeClock()
        )
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), _dispatch("0" * 31 + "8"))
        assert response.status is Status.SERVICE_UNAVAILABLE
        assert client.bodies[0].closed is True
        assert client.bodies[-1].closed is False
        await response.close()

    async def test_async_redirect_loop_returns_open(self) -> None:
        client = _AsyncScriptClient(
            [
                (Status.MOVED_PERMANENTLY, "https://example.com/b"),
                (Status.MOVED_PERMANENTLY, "https://example.com/a"),
            ]
        )
        async with AsyncPipeline(client, policies=[AsyncRedirectPolicy()]) as p:
            response = await p.run(_get("https://example.com/a"), _dispatch("0" * 31 + "9"))
        assert response.status is Status.MOVED_PERMANENTLY
        assert client.bodies[0].closed is True
        assert client.bodies[-1].closed is False
        await response.close()


# --------------------------------------------------------------------------- #
# Transport ownership: the run/fork machinery never closes the transport      #
# --------------------------------------------------------------------------- #


class TestTransportNeverClosedByRun:
    """The ``send`` path never closes the transport; only ``__exit__`` opts in."""

    def test_sync_run_walk_never_closes_transport(self) -> None:
        # A full retry -> redirect walk driven WITHOUT the context manager.
        client = _ScriptClient(
            [
                (Status.SERVICE_UNAVAILABLE, None),
                (Status.MOVED_PERMANENTLY, "https://example.com/new"),
                (Status.OK, None),
            ]
        )
        pipeline = Pipeline(
            client,
            policies=[RedirectPolicy(), RetryPolicy(backoff_factor=0.0, clock=FakeClock())],
        )
        response = pipeline.run(_get(), _dispatch("0" * 30 + "10"))
        assert response.status is Status.OK
        assert len(client.requests) == 3  # the fork walk actually ran
        assert client.close_calls == 0  # ... and never touched transport.close
        response.close()

    def test_sync_context_manager_exit_closes_transport_once(self) -> None:
        # The deliberate opt-in: using the pipeline as a context manager closes
        # the transport exactly once on exit.
        client = _ScriptClient([(Status.OK, None)])
        with Pipeline(client) as p:
            p.run(_get(), _dispatch("0" * 30 + "11")).close()
        assert client.close_calls == 1

    async def test_async_run_walk_never_closes_transport(self) -> None:
        client = _AsyncScriptClient(
            [
                (Status.SERVICE_UNAVAILABLE, None),
                (Status.MOVED_PERMANENTLY, "https://example.com/new"),
                (Status.OK, None),
            ]
        )
        pipeline = AsyncPipeline(
            client,
            policies=[
                AsyncRedirectPolicy(),
                AsyncRetryPolicy(backoff_factor=0.0, clock=_AsyncFakeClock()),
            ],
        )
        response = await pipeline.run(_get(), _dispatch("0" * 30 + "12"))
        assert response.status is Status.OK
        assert len(client.requests) == 3
        assert client.aclose_calls == 0
        await response.close()

    async def test_async_context_manager_exit_closes_transport_once(self) -> None:
        client = _AsyncScriptClient([(Status.OK, None)])
        async with AsyncPipeline(client) as p:
            response = await p.run(_get(), _dispatch("0" * 30 + "13"))
        await response.close()
        assert client.aclose_calls == 1


# --------------------------------------------------------------------------- #
# Options identity preserved across a forced retry + redirect walk            #
# --------------------------------------------------------------------------- #


class TestOptionsIdentityAcrossForks:
    """The per-call ``options`` mapping is the same object at every fork."""

    _SCRIPT: list[_Hop] = [
        (Status.SERVICE_UNAVAILABLE, None),  # retry fork
        (Status.MOVED_PERMANENTLY, "https://example.com/new"),  # redirect fork
        (Status.OK, None),  # final
    ]

    def test_sync_options_identity_stable(self) -> None:
        seen: list[dict[str, Any]] = []
        client = _ScriptClient(self._SCRIPT)
        pipeline = Pipeline(
            client,
            policies=[
                RedirectPolicy(),
                RetryPolicy(backoff_factor=0.0, clock=FakeClock()),
                _OptionsProbe(seen),
            ],
        )
        response = pipeline.run(_get(), _dispatch("0" * 30 + "14"), retry_total=5, marker="abc")
        response.close()
        assert len(seen) == 3  # one observation per fork reaching the transport
        assert all(observed is seen[0] for observed in seen)  # same object every fork
        assert seen[0] == {"retry_total": 5, "marker": "abc"}

    async def test_async_options_identity_stable(self) -> None:
        seen: list[dict[str, Any]] = []
        client = _AsyncScriptClient(self._SCRIPT)
        pipeline = AsyncPipeline(
            client,
            policies=[
                AsyncRedirectPolicy(),
                AsyncRetryPolicy(backoff_factor=0.0, clock=_AsyncFakeClock()),
                _AsyncOptionsProbe(seen),
            ],
        )
        response = await pipeline.run(
            _get(), _dispatch("0" * 30 + "15"), retry_total=5, marker="abc"
        )
        await response.close()
        assert len(seen) == 3
        assert all(observed is seen[0] for observed in seen)
        assert seen[0] == {"retry_total": 5, "marker": "abc"}


# --------------------------------------------------------------------------- #
# Concurrency: 100 parallel sends through one built pipeline, no cross-talk    #
# --------------------------------------------------------------------------- #


class TestConcurrentSendsNoCrossTalk:
    """Concurrent calls through one built pipeline keep per-call state isolated."""

    _N = 100

    def test_sync_100_parallel_sends(self) -> None:
        client = _EchoClient()
        pipeline = Pipeline(client, policies=[_CrossTalkProbe()])
        barrier = threading.Barrier(self._N)

        def one(index: int) -> tuple[int, Response]:
            barrier.wait()  # release all threads simultaneously
            return index, pipeline.run(_get(), _dispatch(f"{index:032x}"), call_id=index)

        with ThreadPoolExecutor(max_workers=self._N) as pool:
            results = list(pool.map(one, range(self._N)))

        for index, response in results:
            assert response.headers.get("X-Echo") == str(index)  # ctx.data isolated
            assert response.headers.get("X-Transport-Saw") == str(index)  # request routed right
            response.close()
        assert client.close_calls == 0  # no close during 100 concurrent runs

    async def test_async_100_parallel_sends(self) -> None:
        client = _AsyncEchoClient()
        pipeline = AsyncPipeline(client, policies=[_AsyncCrossTalkProbe()])

        async def one(index: int) -> tuple[int, AsyncResponse]:
            return index, await pipeline.run(_get(), _dispatch(f"{index:032x}"), call_id=index)

        results = await asyncio.gather(*(one(index) for index in range(self._N)))

        for index, response in results:
            assert response.headers.get("X-Echo") == str(index)
            assert response.headers.get("X-Transport-Saw") == str(index)
            await response.close()
        assert client.aclose_calls == 0
