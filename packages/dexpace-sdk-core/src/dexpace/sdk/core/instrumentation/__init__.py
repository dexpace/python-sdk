# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Instrumentation contracts and no-op defaults.

Exposes the W3C-compatible ``InstrumentationContext``, ``Span``, ``Tracer``,
and ``TracingScope`` types, plus shared no-op singletons (``NOOP_SPAN``,
``NOOP_INSTRUMENTATION_CONTEXT``) for use when tracing is disabled. The
``ClientLogger`` / ``UrlRedactor`` helpers are exposed for use by built-in
pipeline policies. Metrics primitives (``Counter`` / ``Histogram`` /
``MetricsContext``) ship with no-op implementations; real backends live in
sibling packages.
"""

from __future__ import annotations

from .client_logger import ClientLogger, CorrelationFilter
from .correlation import (
    bind_correlation,
    get_span_id,
    get_trace_id,
    set_span_id,
    set_trace_id,
)
from .http_tracer import HttpTracer, HttpTracerFactory
from .identifiers import SpanId, TraceFlags, TraceId, TraceIdType, TraceState
from .instrumentation_context import InstrumentationContext
from .log_level import LogLevel
from .metrics import (
    NOOP_COUNTER,
    NOOP_HISTOGRAM,
    NOOP_METRICS_CONTEXT,
    NOOP_UPDOWN_COUNTER,
    Counter,
    Histogram,
    MetricsContext,
    UpDownCounter,
)
from .noop import (
    NOOP_HTTP_TRACER,
    NOOP_HTTP_TRACER_FACTORY,
    NOOP_INSTRUMENTATION_CONTEXT,
    NOOP_SPAN,
    NOOP_TRACER,
)
from .span import Span
from .tracer import Tracer
from .tracing_scope import TracingScope
from .url_redactor import DEFAULT_QUERY_ALLOWLIST, UrlRedactor

__all__ = [
    "DEFAULT_QUERY_ALLOWLIST",
    "NOOP_COUNTER",
    "NOOP_HISTOGRAM",
    "NOOP_HTTP_TRACER",
    "NOOP_HTTP_TRACER_FACTORY",
    "NOOP_INSTRUMENTATION_CONTEXT",
    "NOOP_METRICS_CONTEXT",
    "NOOP_SPAN",
    "NOOP_TRACER",
    "NOOP_UPDOWN_COUNTER",
    "ClientLogger",
    "CorrelationFilter",
    "Counter",
    "Histogram",
    "HttpTracer",
    "HttpTracerFactory",
    "InstrumentationContext",
    "LogLevel",
    "MetricsContext",
    "Span",
    "SpanId",
    "TraceFlags",
    "TraceId",
    "TraceIdType",
    "TraceState",
    "Tracer",
    "TracingScope",
    "UpDownCounter",
    "UrlRedactor",
    "bind_correlation",
    "get_span_id",
    "get_trace_id",
    "set_span_id",
    "set_trace_id",
]
