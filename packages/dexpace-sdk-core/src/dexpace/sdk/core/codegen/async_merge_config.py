# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of `merge_config.merge_config`.

Folds `ServiceDefaults` + a user override into `AsyncServiceCore`'s kwargs the
same way the sync twin folds them into `ServiceCore`'s — one
`default_async_pipeline(...)` call, the service's User-Agent token fed into
`AsyncClientIdentityPolicy`, and the api-version stamp appended at
`Stage.PRE_SEND`. See `merge_config.py` for the full rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..pipeline.defaults import default_async_pipeline
from ..pipeline.policies.async_client_identity import AsyncClientIdentityPolicy
from ..pipeline.policies.client_identity import default_user_agent
from ._async_api_version_policy import AsyncApiVersionPolicy
from .service_defaults import DEFAULT_API_VERSION_HEADER, ServiceDefaults, merge_service_defaults

if TYPE_CHECKING:
    from ..client.async_http_client import AsyncHttpClient
    from ..pipeline.async_pipeline import AsyncPipeline
    from ..pipeline.async_policy import AsyncPolicy
    from ..pipeline.policies.async_idempotency import AsyncIdempotencyPolicy
    from ..pipeline.policies.async_logging_policy import AsyncLoggingPolicy
    from ..pipeline.policies.async_redirect import AsyncRedirectPolicy
    from ..pipeline.policies.async_retry import AsyncRetryPolicy
    from ..pipeline.policies.async_set_date import AsyncSetDatePolicy
    from ..pipeline.policies.async_tracing_policy import AsyncTracingPolicy

__all__ = ["merge_async_config"]


def merge_async_config(
    defaults: ServiceDefaults,
    user: ServiceDefaults | None = None,
    *,
    transport: AsyncHttpClient | None = None,
    pipeline: AsyncPipeline | None = None,
    redirect: AsyncRedirectPolicy | None = None,
    idempotency: AsyncIdempotencyPolicy | None = None,
    retry: AsyncRetryPolicy | None = None,
    set_date: AsyncSetDatePolicy | None = None,
    auth: AsyncPolicy | None = None,
    logging: AsyncLoggingPolicy | None = None,
    tracing: AsyncTracingPolicy | None = None,
) -> dict[str, Any]:
    """Build `AsyncServiceCore`'s constructor kwargs from service + user config.

    Args:
        defaults: The service's own baked-in defaults.
        user: The caller's override of the same shape. A non-``None`` field
            here always wins; a ``None`` field falls through to ``defaults``.
        transport: An async transport to build the one customized
            `default_async_pipeline` around. Mutually exclusive with
            ``pipeline``.
        pipeline: A caller-supplied, already-built `AsyncPipeline` — the BYO /
            shared-transport case. Passed through unmodified. Mutually
            exclusive with ``transport``.
        redirect: Forwarded to `default_async_pipeline` when building around
            ``transport``. Ignored when ``pipeline`` is given.
        idempotency: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.
        retry: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.
        set_date: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.
        auth: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.
        logging: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.
        tracing: Forwarded to `default_async_pipeline`. Ignored when
            ``pipeline`` is given.

    Returns:
        A mapping with ``base_url`` / ``pipeline`` / ``errors`` / ``serde``
        keys ready to splat into ``AsyncServiceCore(**merge_async_config(...))``.

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
    transport: AsyncHttpClient,
    *,
    redirect: AsyncRedirectPolicy | None,
    idempotency: AsyncIdempotencyPolicy | None,
    retry: AsyncRetryPolicy | None,
    set_date: AsyncSetDatePolicy | None,
    auth: AsyncPolicy | None,
    logging: AsyncLoggingPolicy | None,
    tracing: AsyncTracingPolicy | None,
) -> AsyncPipeline:
    """Build the single async pipeline `merge_async_config` hands to `AsyncServiceCore`.

    Exactly one `default_async_pipeline(...)` call, exactly one `.build()`.
    """
    client_identity = None
    if merged.user_agent_token is not None:
        client_identity = AsyncClientIdentityPolicy(
            user_agent=f"{merged.user_agent_token} {default_user_agent()}"
        )
    builder = default_async_pipeline(
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
        builder.append(AsyncApiVersionPolicy(header, merged.api_version))
    return builder.build()
