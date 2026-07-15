# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Build a concrete `Request` from a declarative `Operation` and `OperationInput`."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import quote

from ..http.common import Headers, QueryParams, Url
from ..http.request import Request

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..serde import Serde
    from .operation import Operation, OperationInput

__all__ = ["assemble"]

#: Matches a single ``{name}`` path placeholder; ``name`` excludes braces so a
#: malformed ``{`` or ``}`` is left untouched rather than mis-parsed.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def assemble(
    op: Operation,
    input: OperationInput,
    base_url: str | Url,
    *,
    serde: Serde | None = None,
) -> Request:
    """Build a concrete `Request` from a declarative operation description.

    Substitutes the operation's path template against ``input.path_params``,
    joins it onto ``base_url``, appends ``input.query`` after any query already
    on the base URL, overlays per-call headers on the operation's static
    headers, and carries the body through unchanged. The operation's ``name`` is
    advisory and never affects the result.

    Args:
        op: The static operation description (method, path template, headers).
        input: The per-call values (path params, query, headers, body).
        base_url: The absolute base URL, as a string or `Url`. Any query it
            carries (e.g. a SAS / signed-URL token) is preserved; it must not
            carry a URL fragment.
        serde: Reserved for a later executor layer that serializes the body and
            derives a ``Content-Type``. This assembly layer carries the body
            through verbatim and does not consult it.

    Returns:
        The assembled `Request`.

    Raises:
        KeyError: If the path template names a placeholder with no matching
            entry in ``input.path_params``; the missing name is reported.
        ValueError: If ``base_url`` carries a URL fragment or is not a valid
            absolute URL.
    """
    base = _base_url(base_url)
    url = replace(
        base,
        path=_join_path(base.path, _render_path(op.path, input.path_params)),
        query=_append_query(base.query, input.query),
    )
    return Request(
        method=op.method,
        url=url,
        headers=_merge_headers(op.headers, input.headers),
        body=input.body,
    )


def _base_url(base_url: str | Url) -> Url:
    """Parse and validate the base URL, rejecting fragments and malformed input.

    Args:
        base_url: The base URL as a string or `Url`.

    Returns:
        The validated `Url`.

    Raises:
        ValueError: If ``base_url`` is not a valid absolute URL or carries a
            fragment. The raw value is not echoed so a signed-URL secret cannot
            leak through the message.
    """
    try:
        base = Url.parse(base_url) if isinstance(base_url, str) else base_url
    except ValueError as exc:
        raise ValueError(
            "assemble: base_url is not a valid absolute URL (a scheme and host are required)"
        ) from exc
    if base.fragment:
        raise ValueError("assemble: base_url must not carry a URL fragment ('#...')")
    return base


def _render_path(template: str, params: Mapping[str, str]) -> str:
    """Substitute every ``{placeholder}`` with its percent-encoded value.

    Each value is encoded with ``safe=""`` so only RFC 3986 ``unreserved``
    characters survive verbatim; a literal ``/`` becomes ``%2F`` and therefore
    stays within a single path segment rather than introducing a new one.

    Args:
        template: The path template, possibly containing ``{name}`` tokens.
        params: The values to substitute, keyed by placeholder name.

    Returns:
        The template with every placeholder substituted.

    Raises:
        KeyError: If a placeholder has no matching key in ``params``; only the
            missing name is reported, never the supplied values.
    """

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(
                f"assemble: missing path parameter {name!r} for placeholder "
                f"'{{{name}}}' in path template {template!r}"
            )
        return quote(params[name], safe="")

    return _PLACEHOLDER.sub(_substitute, template)


def _join_path(base_path: str, op_path: str) -> str:
    """Join base and operation paths with exactly one separating slash.

    Collapses the seam so a trailing slash on the base and a leading slash on
    the operation path never produce a doubled ``//``; a trailing slash on the
    operation path itself is preserved.

    Args:
        base_path: The path component of the base URL.
        op_path: The substituted operation path.

    Returns:
        The combined path.
    """
    left = base_path.rstrip("/")
    right = op_path.lstrip("/")
    if not right:
        return left or "/"
    return f"{left}/{right}"


def _append_query(base: QueryParams, extra: QueryParams) -> QueryParams:
    """Append the operation's query parameters after those already on the base URL.

    Args:
        base: The query parameters already present on the base URL.
        extra: The operation's own query parameters.

    Returns:
        The base parameters with ``extra`` appended, preserving order and
        duplicates. Rendering happens later via the pure RFC 3986 query codec
        that `QueryParams` uses.
    """
    if not len(extra):
        return base
    return QueryParams([*base.flatten(), *extra.flatten()])


def _merge_headers(base: Headers, override: Headers) -> Headers:
    """Overlay per-call headers on the operation's static headers.

    For each name present in ``override`` the base values for that name are
    replaced outright (per-call wins); names only in ``base`` are kept.

    Args:
        base: The operation's static headers.
        override: The per-call headers.

    Returns:
        The merged headers.
    """
    if not len(override):
        return base
    merged = base
    for name in override.names():
        merged = merged.with_set(name, *override.values(name))
    return merged
