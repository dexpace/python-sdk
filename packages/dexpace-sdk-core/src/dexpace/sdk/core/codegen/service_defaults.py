# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Baked-in service-level configuration layered onto the single core pipeline.

`ServiceDefaults` is the Tier-2 seam: an enterprise/service SDK generated on
top of this toolkit bakes its own defaults (base URL, product User-Agent
token, api-version stamp, error map, wire serde) into one instance, and
`merge_config` / `merge_async_config` (in `merge_config.py` /
`async_merge_config.py`) fold it — plus an optional user-supplied override of
the same shape — into the single core pipeline's constructor kwargs. There is
never a second pipeline and never a wrapper client: the service's
customizations are policies appended to the same `default_pipeline` /
`default_async_pipeline` call that builds the executor's one pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ..http.common import Url
    from ..serde import Serde
    from .status_error_map import StatusErrorMap

__all__ = ["DEFAULT_API_VERSION_HEADER", "ServiceDefaults"]

#: Header an `api_version` stamp is written under when `ServiceDefaults`
#: leaves `api_version_header` unset.
DEFAULT_API_VERSION_HEADER: Final[str] = "Api-Version"


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceDefaults:
    """A service's baked-in defaults for its generated client.

    Every field defaults to ``None`` — "no opinion, fall through to whatever
    the next layer supplies". `merge_config` / `merge_async_config` resolve a
    `ServiceDefaults` (the service's own baked-in choices) against an optional
    second `ServiceDefaults` (the end caller's overrides) field-wise: a
    non-``None`` field on the caller's override always wins, a non-``None``
    field on the service defaults wins next, and the toolkit's own defaults
    (`default_pipeline`'s fallbacks, `ServiceCore`'s fallbacks) apply only when
    both leave a field unset.

    Attributes:
        base_url: The service's default base URL. A caller override replaces
            it outright; one of the two must resolve to non-``None`` or
            `merge_config` raises.
        user_agent_token: The service's own product token (for example
            ``"acme-sdk/2.3"``). Fed into `ClientIdentityPolicy` as the
            leading token, so it is PREPENDED to the platform
            (``dexpace-sdk/<ver>``) and runtime (``python/<ver>``) tokens the
            policy already appends — the ordered list reads service, then
            platform, then runtime.
        api_version: The api-version value to stamp on every outgoing
            request, or ``None`` to stamp nothing. Applied by a policy at the
            innermost non-terminal pipeline stage, so it is re-stamped on
            every redirect hop and every retry attempt.
        api_version_header: The header name the `api_version` value is
            stamped under. ``None`` falls back to `DEFAULT_API_VERSION_HEADER`.
        errors: The service's default status -> typed-error map, forwarded to
            `ServiceCore` / `AsyncServiceCore` verbatim.
        serde: The service's default wire-format serde, forwarded to
            `ServiceCore` / `AsyncServiceCore` verbatim.
    """

    base_url: str | Url | None = None
    user_agent_token: str | None = None
    api_version: str | None = None
    api_version_header: str | None = None
    errors: StatusErrorMap | None = None
    serde: Serde | None = None


def merge_service_defaults(
    defaults: ServiceDefaults, user: ServiceDefaults | None
) -> ServiceDefaults:
    """Field-wise merge: every non-``None`` field on ``user`` wins over ``defaults``.

    Args:
        defaults: The service's own baked-in defaults.
        user: The caller's override, or ``None`` for no override at all (in
            which case ``defaults`` is returned unchanged).

    Returns:
        A `ServiceDefaults` with each field resolved independently — the
        precedence is per-field, not per-object, so a caller overriding only
        ``base_url`` still inherits the service's ``user_agent_token``.
    """
    if user is None:
        return defaults
    overrides = {
        f.name: value
        for f in fields(ServiceDefaults)
        if (value := getattr(user, f.name)) is not None
    }
    return replace(defaults, **overrides)
