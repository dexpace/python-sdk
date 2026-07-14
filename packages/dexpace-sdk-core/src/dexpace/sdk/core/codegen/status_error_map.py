# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Data-driven status-code → typed-error mapping for generated clients.

`StatusErrorMap` is the declarative half of a generated client's error-handling
contract. It maps HTTP status codes to the `HttpResponseError` subclasses a
service defines, with a fallback ``default`` class for any error status not
named explicitly. The map is frozen once built and carries no branching logic
itself — `raise_for` and `for_status` read it as data, so a service SDK never
grows a hand-written ``if status == ...: raise`` chain.

The map spans the *response* branch of the error taxonomy only: every registered
class must extend `HttpResponseError` and must NOT extend `OSError`. That is
enforced at construction (`TypeError` otherwise) and is the hard rule a service
taxonomy relies on — a service-specific error can never masquerade as a
transport-level `NetworkError`, so the map never catches a `NetworkError` and
re-wraps it onto the response branch. `except OSError:` call sites keep matching
transport failures unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn

from ..errors.http import HttpResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..http.response.response import Response

__all__ = ["StatusErrorMap"]


def _validate_branch(error_class: type[HttpResponseError]) -> None:
    """Reject an error class that is not purely on the response branch.

    Args:
        error_class: A candidate class registered in a `StatusErrorMap` (either
            a per-status override or the ``default``).

    Raises:
        TypeError: If `error_class` is not a subclass of `HttpResponseError`, or
            if it also subclasses `OSError` — which would place it on the
            transport / `NetworkError` branch and let an `except OSError:` site
            catch a response-branch error.
    """
    if not (isinstance(error_class, type) and issubclass(error_class, HttpResponseError)):
        raise TypeError(
            f"StatusErrorMap error classes must subclass HttpResponseError; got {error_class!r}"
        )
    if issubclass(error_class, OSError):
        raise TypeError(
            "StatusErrorMap error classes must not subclass OSError "
            f"(the NetworkError / transport branch); got {error_class!r}"
        )


def _is_error_status(status: int) -> bool:
    """Return whether `status` is a 4xx or 5xx HTTP error code.

    Args:
        status: An HTTP status code.

    Returns:
        ``True`` for ``400 <= status < 600`` (client or server error),
        matching `Status.is_error`; ``False`` for informational, success, and
        redirect codes.
    """
    return 400 <= status < 600


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusErrorMap:
    """A frozen status-code → typed-error mapping consumed as data.

    Keyword-only, frozen, and slotted so a future minor version can append a
    field without breaking positional call sites and the built map is immutable.
    ``by_status`` is defensively copied into a read-only view at construction, so
    mutating the caller's original dict afterwards never affects the map.

    Attributes:
        by_status: Per-status overrides. A status present here wins over
            ``default``. Keys may be plain ints or `Status` members (a `Status`
            is an ``int``); values must be `HttpResponseError` subclasses that
            are not also `OSError` subclasses.
        default: The fallback error class for any error status not named in
            ``by_status``. Defaults to `HttpResponseError`.
    """

    by_status: Mapping[int, type[HttpResponseError]] = field(default_factory=dict)
    default: type[HttpResponseError] = HttpResponseError

    def __post_init__(self) -> None:
        """Validate branch membership and freeze ``by_status`` into a read-only view.

        Raises:
            TypeError: If ``default`` or any ``by_status`` value is off the
                response branch (see `_validate_branch`).
        """
        _validate_branch(self.default)
        frozen: dict[int, type[HttpResponseError]] = {}
        for status, error_class in self.by_status.items():
            _validate_branch(error_class)
            frozen[int(status)] = error_class
        object.__setattr__(self, "by_status", MappingProxyType(frozen))

    def for_status(self, status: int) -> type[HttpResponseError] | None:
        """Return the error class a status *would* raise, without raising.

        The permissive counterpart to `raise_for`: it never raises and never
        buffers a body, so a caller can peek at the error taxonomy for a status
        without triggering the full raise path. A non-error status returns
        ``None``; an error status returns its per-status override if one is
        registered, otherwise ``default``.

        Args:
            status: The HTTP status code to look up — a plain ``int`` or a
                `Status` member.

        Returns:
            The mapped `HttpResponseError` subclass for a 4xx/5xx status, or
            ``None`` when `status` is not an error code.
        """
        if not _is_error_status(status):
            return None
        mapped: type[HttpResponseError] | None = self.by_status.get(int(status))
        return mapped if mapped is not None else self.default

    def raise_for(self, response: Response) -> NoReturn:
        """Raise the typed error mapped to ``response``'s status.

        The strict counterpart to `for_status`: it always raises. Calling it on
        a non-error (2xx / 3xx / 1xx) response is a caller error — it raises
        `ValueError` rather than fabricating a success-looking exception. For an
        error response it constructs the mapped error (the per-status override,
        else ``default``), buffers a bounded, replayable copy of the response
        body onto it, then raises it.

        Args:
            response: The received response to raise for. Its status must be a
                4xx or 5xx error.

        Raises:
            ValueError: If ``response``'s status is not a 4xx/5xx error.
            HttpResponseError: The mapped error, carrying ``response`` and its
                bounded, replayable buffered body.
        """
        status = response.status
        if not status.is_error:
            raise ValueError(
                f"raise_for requires an error (4xx/5xx) response; got a non-error status {status!r}"
            )
        mapped: type[HttpResponseError] | None = self.by_status.get(int(status))
        error_class = mapped if mapped is not None else self.default
        error = error_class(response=response)
        error.read_body()  # buffer a bounded, replayable copy before it propagates
        raise error
