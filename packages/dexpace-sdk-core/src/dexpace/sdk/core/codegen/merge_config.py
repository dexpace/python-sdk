# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Fold `ServiceDefaults` + a user override into `ServiceCore`'s kwargs.

`merge_config` is the Tier-2 flattening step: it never builds a second
pipeline and never wraps one client in another. Given a transport, it makes
exactly one `default_pipeline(...)` call — with the service's User-Agent
token and api-version stamp folded in as ordinary policy kwargs/appends — and
returns the resulting single `Pipeline` under the `pipeline` key so
`ServiceCore(**merge_config(...))` borrows it. Given a caller-supplied
already-built `Pipeline` instead (the BYO / shared-transport case), the
pipeline passes through untouched: nesting is reserved for that case alone,
and even then `ServiceCore`'s existing ownership-aware close means the shared
transport is never closed by the executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..pipeline.defaults import default_pipeline
from ..pipeline.policies.client_identity import ClientIdentityPolicy, default_user_agent
from ._api_version_policy import ApiVersionPolicy
from .service_defaults import DEFAULT_API_VERSION_HEADER, ServiceDefaults, merge_service_defaults

if TYPE_CHECKING:
    from ..client.http_client import HttpClient
    from ..pipeline.pipeline import Pipeline
    from ..pipeline.policies.idempotency import IdempotencyPolicy
    from ..pipeline.policies.logging_policy import LoggingPolicy
    from ..pipeline.policies.redirect import RedirectPolicy
    from ..pipeline.policies.retry import RetryPolicy
    from ..pipeline.policies.set_date import SetDatePolicy
    from ..pipeline.policies.tracing_policy import TracingPolicy
    from ..pipeline.policy import Policy

__all__ = ["merge_config"]


def merge_config(
    defaults: ServiceDefaults,
    user: ServiceDefaults | None = None,
    *,
    transport: HttpClient | None = None,
    pipeline: Pipeline | None = None,
    redirect: RedirectPolicy | None = None,
    idempotency: IdempotencyPolicy | None = None,
    retry: RetryPolicy | None = None,
    set_date: SetDatePolicy | None = None,
    auth: Policy | None = None,
    logging: LoggingPolicy | None = None,
    tracing: TracingPolicy | None = None,
) -> dict[str, Any]:
    """Build `ServiceCore`'s constructor kwargs from service + user config.

    Args:
        defaults: The service's own baked-in defaults.
        user: The caller's override of the same shape. A non-``None`` field
            here always wins; a ``None`` field falls through to ``defaults``.
        transport: A transport to build the one customized `default_pipeline`
            around. Mutually exclusive with ``pipeline``.
        pipeline: A caller-supplied, already-built `Pipeline` — the BYO /
            shared-transport case. Passed through unmodified: service
            defaults cannot be layered onto an opaque pipeline, so use
            ``transport=`` if the api-version stamp or User-Agent token must
            apply. Mutually exclusive with ``transport``.
        redirect: Forwarded to `default_pipeline` when building around
            ``transport``. Ignored when ``pipeline`` is given.
        idempotency: Forwarded to `default_pipeline`. Ignored when
            ``pipeline`` is given.
        retry: Forwarded to `default_pipeline`. Ignored when ``pipeline`` is
            given.
        set_date: Forwarded to `default_pipeline`. Ignored when ``pipeline``
            is given.
        auth: Forwarded to `default_pipeline`. Ignored when ``pipeline`` is
            given.
        logging: Forwarded to `default_pipeline`. Ignored when ``pipeline``
            is given.
        tracing: Forwarded to `default_pipeline`. Ignored when ``pipeline``
            is given.

    Returns:
        A mapping with ``base_url`` / ``pipeline`` / ``errors`` / ``serde``
        keys ready to splat into ``ServiceCore(**merge_config(...))``.

    Raises:
        ValueError: If neither or both of ``transport`` and ``pipeline`` are
            given, or if neither ``defaults`` nor ``user`` resolves a
            ``base_url``.
    """
    if (transport is None) == (pipeline is None):
        raise ValueError("provide exactly one of transport= or pipeline=")
    merged = merge_service_defaults(defaults, user)
    if merged.base_url is None:
        raise ValueError("base_url must be set by service defaults or user config")
    if pipeline is not None:
        resolved_pipeline = pipeline
    else:
        assert transport is not None  # narrowed by the exclusivity guard above
        resolved_pipeline = _build_pipeline(
            merged,
            transport,
            redirect=redirect,
            idempotency=idempotency,
            retry=retry,
            set_date=set_date,
            auth=auth,
            logging=logging,
            tracing=tracing,
        )
    return {
        "base_url": merged.base_url,
        "pipeline": resolved_pipeline,
        "errors": merged.errors,
        "serde": merged.serde,
    }


def _build_pipeline(
    merged: ServiceDefaults,
    transport: HttpClient,
    *,
    redirect: RedirectPolicy | None,
    idempotency: IdempotencyPolicy | None,
    retry: RetryPolicy | None,
    set_date: SetDatePolicy | None,
    auth: Policy | None,
    logging: LoggingPolicy | None,
    tracing: TracingPolicy | None,
) -> Pipeline:
    """Build the single pipeline `merge_config` hands to `ServiceCore` as BYO.

    Exactly one `default_pipeline(...)` call, exactly one `.build()` — the
    service's User-Agent token and api-version stamp are folded in as an
    ordinary `client_identity` kwarg and a `PRE_SEND` append, never a second
    pipeline or a policy fork.
    """
    client_identity = None
    if merged.user_agent_token is not None:
        client_identity = ClientIdentityPolicy(
            user_agent=f"{merged.user_agent_token} {default_user_agent()}"
        )
    builder = default_pipeline(
        transport,
        redirect=redirect,
        idempotency=idempotency,
        retry=retry,
        set_date=set_date,
        client_identity=client_identity,
        auth=auth,
        logging=logging,
        tracing=tracing,
    )
    if merged.api_version is not None:
        header = merged.api_version_header or DEFAULT_API_VERSION_HEADER
        builder.append(ApiVersionPolicy(header, merged.api_version))
    return builder.build()
