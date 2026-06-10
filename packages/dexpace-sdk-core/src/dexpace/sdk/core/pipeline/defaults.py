# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Convenience factories that wire the canonical policy stack."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .async_staged_builder import AsyncStagedPipelineBuilder
from .policies.async_client_identity import AsyncClientIdentityPolicy
from .policies.async_idempotency import AsyncIdempotencyPolicy
from .policies.async_redirect import AsyncRedirectPolicy
from .policies.async_retry import AsyncRetryPolicy
from .policies.async_set_date import AsyncSetDatePolicy
from .policies.client_identity import ClientIdentityPolicy
from .policies.idempotency import IdempotencyPolicy
from .policies.logging_policy import LoggingPolicy
from .policies.redirect import RedirectPolicy
from .policies.retry import RetryPolicy
from .policies.set_date import SetDatePolicy
from .policies.tracing_policy import TracingPolicy
from .staged_builder import StagedPipelineBuilder

if TYPE_CHECKING:
    from ..client.async_http_client import AsyncHttpClient
    from ..client.http_client import HttpClient
    from .async_policy import AsyncPolicy
    from .policy import Policy


def default_pipeline(
    client: HttpClient,
    *,
    redirect: RedirectPolicy | None = None,
    idempotency: IdempotencyPolicy | None = None,
    retry: RetryPolicy | None = None,
    set_date: SetDatePolicy | None = None,
    client_identity: ClientIdentityPolicy | None = None,
    auth: Policy | None = None,
    logging: LoggingPolicy | None = None,
    tracing: TracingPolicy | None = None,
) -> StagedPipelineBuilder:
    """Pre-configured `StagedPipelineBuilder` with the canonical stack.

    Wires the policies that most consumers want by default in the order their
    stages dictate: redirect → idempotency → retry → set-date →
    client-identity → auth → logging → tracing. Each policy is opt-out (pass
    ``None``) or opt-in-with-override (pass a pre-configured instance to
    replace the default).

    Idempotency sits before retry so a write request's ``Idempotency-Key`` is
    minted once and reused across every retry; ``set-date`` and
    ``client-identity`` sit just inside the retry wrapper.

    Args:
        client: Terminal HTTP transport.
        redirect: Override for `RedirectPolicy`. ``None`` uses defaults.
        idempotency: Override for `IdempotencyPolicy`. ``None`` uses
            defaults.
        retry: Override for `RetryPolicy`. ``None`` uses defaults.
        set_date: Override for `SetDatePolicy`. ``None`` uses defaults.
        client_identity: Override for `ClientIdentityPolicy`. ``None``
            uses defaults.
        auth: Optional authentication policy (``BearerTokenPolicy``,
            ``BasicAuthPolicy``, ``KeyCredentialPolicy``, etc.). No default —
            requests pass without authentication when this is ``None``.
        logging: Override for `LoggingPolicy`. ``None`` uses defaults.
        tracing: Override for `TracingPolicy`. ``None`` uses defaults.

    Returns:
        A `StagedPipelineBuilder` ready for further customisation or
        immediate ``.build()``.
    """
    builder = StagedPipelineBuilder(client)
    builder.append(redirect or RedirectPolicy())
    builder.append(idempotency or IdempotencyPolicy())
    builder.append(retry or RetryPolicy())
    builder.append(set_date or SetDatePolicy())
    builder.append(client_identity or ClientIdentityPolicy())
    if auth is not None:
        builder.append(auth)
    builder.append(logging or LoggingPolicy())
    builder.append(tracing or TracingPolicy())
    return builder


def default_async_pipeline(
    client: AsyncHttpClient,
    *,
    redirect: AsyncRedirectPolicy | None = None,
    idempotency: AsyncIdempotencyPolicy | None = None,
    retry: AsyncRetryPolicy | None = None,
    set_date: AsyncSetDatePolicy | None = None,
    client_identity: AsyncClientIdentityPolicy | None = None,
    auth: AsyncPolicy | None = None,
) -> AsyncStagedPipelineBuilder:
    """Async twin of `default_pipeline`.

    Mirrors the sync version's stack minus logging/tracing, which currently
    only ship as sync policies. Async-side observability lives on the caller's
    side until async versions land.
    """
    builder = AsyncStagedPipelineBuilder(client)
    builder.append(redirect or AsyncRedirectPolicy())
    builder.append(idempotency or AsyncIdempotencyPolicy())
    builder.append(retry or AsyncRetryPolicy())
    builder.append(set_date or AsyncSetDatePolicy())
    builder.append(client_identity or AsyncClientIdentityPolicy())
    if auth is not None:
        builder.append(auth)
    return builder


__all__ = ["default_async_pipeline", "default_pipeline"]
