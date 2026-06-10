# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Digest access authentication handler (RFC 7616).

Supports algorithms ``MD5``, ``MD5-sess``, ``SHA-256``, and ``SHA-256-sess``
with ``qop=auth``. ``auth-int`` and mutual-auth (``Authentication-Info``)
verification are out of scope — matching the Java v1 cut.

A single handler instance is intended to be reused across requests so the
per-client nonce counter (``nc``) advances monotonically. The counter is
guarded by a ``threading.Lock`` since CPython integer increment is not
atomic with respect to other threads' reads of the same variable.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..common.url import Url
from ..request.method import Method
from .challenge import AuthenticateChallenge

# Type alias for the algorithm parameter strings recognised on the wire.
DigestAlgorithm = str


@dataclass(frozen=True, slots=True)
class _ResolvedChallenge:
    """The validated parameters extracted from a selected Digest challenge."""

    algorithm: str
    algo_key: str
    hasher: Callable[[bytes], hashlib._Hash]
    realm: str
    nonce: str
    opaque: str | None
    charset: str
    qop: str | None


_DEFAULT_PREFERENCE: Final[tuple[DigestAlgorithm, ...]] = (
    "SHA-256-sess",
    "SHA-256",
    "MD5-sess",
    "MD5",
)

_HASHERS: Final[dict[str, Callable[[bytes], hashlib._Hash]]] = {
    "MD5": hashlib.md5,
    "MD5-SESS": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-256-SESS": hashlib.sha256,
}


class DigestChallengeHandler:
    """Satisfy a ``Digest`` challenge per RFC 7616.

    Args:
        username: Identity sent in the ``username`` parameter.
        password: Secret used to compute ``HA1``.
        preferred_algorithms: Ordered preference for the algorithm parameter
            when the server offers more than one Digest challenge. The
            first preference matched against an offered challenge wins. The
            tuple also acts as an allow-list: an offered algorithm outside it
            is never selected, even as a fallback, so narrowing the tuple to
            ``("SHA-256",)`` declines an ``MD5``-only server. The default
            contains every supported algorithm, so ``MD5`` stays reachable.
        cnonce_factory: Override for the client nonce generator. Defaults
            to ``secrets.token_hex(16)``; tests inject a deterministic
            value to assert against RFC fixtures.
    """

    __slots__ = (
        "_cnonce_factory",
        "_counter",
        "_lock",
        "_password",
        "_preferred",
        "_username",
    )

    def __init__(
        self,
        username: str,
        password: str,
        *,
        preferred_algorithms: tuple[DigestAlgorithm, ...] = _DEFAULT_PREFERENCE,
        cnonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("username and password must be strings")
        if not preferred_algorithms:
            raise ValueError("preferred_algorithms must not be empty")
        self._username = username
        self._password = password
        self._preferred = preferred_algorithms
        self._cnonce_factory = cnonce_factory or (lambda: secrets.token_hex(16))
        self._counter = 0
        self._lock = threading.Lock()

    def can_handle(self, challenges: list[AuthenticateChallenge]) -> bool:
        return self._select(challenges) is not None

    def handle(
        self,
        method: Method,
        url: Url,
        challenges: list[AuthenticateChallenge],
        *,
        is_proxy: bool,
    ) -> tuple[str, str] | None:
        selected = self._select(challenges)
        if selected is None:
            return None
        resolved = self._resolve(selected)
        if resolved is None:
            return None
        nc = self._next_nc()
        cnonce = self._cnonce_factory()
        uri = _request_uri(url)
        response = self._compute_response(
            hasher=resolved.hasher,
            algorithm=resolved.algo_key,
            method=str(method),
            uri=uri,
            realm=resolved.realm,
            nonce=resolved.nonce,
            nc=nc,
            cnonce=cnonce,
            qop=resolved.qop,
            charset=resolved.charset,
        )
        header_value = _format_header(
            username=self._username,
            realm=resolved.realm,
            nonce=resolved.nonce,
            uri=uri,
            response=response,
            algorithm=resolved.algorithm,
            cnonce=cnonce,
            nc=nc,
            qop=resolved.qop,
            opaque=resolved.opaque,
        )
        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        return header_name, header_value

    def _resolve(self, selected: AuthenticateChallenge) -> _ResolvedChallenge | None:
        """Validate and extract the parameters needed to answer ``selected``.

        Returns ``None`` (decline) when the algorithm is unsupported, the
        credentials are not encodable under the negotiated charset, or the
        server advertised a ``qop`` we do not implement.
        """
        algorithm = selected.parameters.get("algorithm", "MD5")
        algo_key = algorithm.upper()
        hasher = _HASHERS.get(algo_key)
        if hasher is None:
            return None
        charset = _select_charset(selected.parameters.get("charset"))
        if not self._credentials_encodable(charset):
            # A charset-less challenge defaults to ISO-8859-1, which cannot
            # represent CJK/emoji credentials. Decline rather than letting a
            # ``UnicodeEncodeError`` escape the documented contract.
            return None
        qop = self._pick_qop(selected.parameters.get("qop"))
        if qop is None and "qop" in selected.parameters:
            # The server advertised qop but did not include ``auth``: we
            # only implement auth, so we cannot satisfy this challenge.
            return None
        if qop is None and algo_key.endswith("-SESS"):
            # A session variant folds ``cnonce`` into HA1, but without ``qop``
            # the response header omits ``cnonce``/``nc`` (RFC 7616 §3.4), so
            # the server cannot reconstruct HA1. Such a challenge is
            # self-contradictory (session variants were introduced alongside
            # qop); decline rather than emit an unverifiable header.
            return None
        return _ResolvedChallenge(
            algorithm=algorithm,
            algo_key=algo_key,
            hasher=hasher,
            realm=selected.parameters.get("realm", ""),
            nonce=selected.parameters.get("nonce", ""),
            opaque=selected.parameters.get("opaque"),
            charset=charset,
            qop=qop,
        )

    def _select(self, challenges: list[AuthenticateChallenge]) -> AuthenticateChallenge | None:
        """Return the best Digest challenge per ``preferred_algorithms``.

        ``preferred_algorithms`` is both a preference order and an allow-list:
        an offered algorithm is only accepted if it appears in the tuple. The
        first preference matched against an offered challenge wins; failing an
        exact-preference match, the first challenge whose algorithm is still
        within the allow-list is taken. The default preference contains every
        supported algorithm, so ``MD5`` remains reachable unless the caller
        narrows the tuple to exclude it.
        """
        digest_challenges = [c for c in challenges if c.scheme.casefold() == "digest"]
        if not digest_challenges:
            return None
        allowed = {preferred.upper() for preferred in self._preferred}
        # First, match by preference order.
        for preferred in self._preferred:
            target = preferred.upper()
            for challenge in digest_challenges:
                algo = challenge.parameters.get("algorithm", "MD5").upper()
                if algo == target and algo in _HASHERS:
                    return challenge
        # Fallback: the first challenge whose algorithm we recognise *and*
        # the caller allowed via ``preferred_algorithms``.
        for challenge in digest_challenges:
            algo = challenge.parameters.get("algorithm", "MD5").upper()
            if algo in _HASHERS and algo in allowed:
                return challenge
        return None

    def _next_nc(self) -> str:
        with self._lock:
            # Clamp to 32 bits — ``nc`` is rendered as 8 hex digits per
            # RFC 7616, and wrapping after 2**32-1 is acceptable since the
            # server hashes the value (no monotonic check).
            self._counter = (self._counter + 1) & 0xFFFFFFFF
            value = self._counter
        return f"{value:08x}"

    def _credentials_encodable(self, charset: str) -> bool:
        """Report whether username/password survive ``charset`` encoding."""
        try:
            self._username.encode(charset)
            self._password.encode(charset)
        except UnicodeEncodeError:
            return False
        return True

    @staticmethod
    def _pick_qop(qop_param: str | None) -> str | None:
        if qop_param is None:
            return None
        options = [opt.strip().lower() for opt in qop_param.split(",")]
        return "auth" if "auth" in options else None

    def _compute_response(
        self,
        *,
        hasher: Callable[[bytes], hashlib._Hash],
        algorithm: str,
        method: str,
        uri: str,
        realm: str,
        nonce: str,
        nc: str,
        cnonce: str,
        qop: str | None,
        charset: str,
    ) -> str:
        def h(data: str) -> str:
            return hasher(data.encode(charset)).hexdigest()

        ha1 = h(f"{self._username}:{realm}:{self._password}")
        if algorithm.endswith("-SESS"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        if qop == "auth":
            return h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        # No qop (legacy RFC 2069): response = H(HA1:nonce:HA2)
        return h(f"{ha1}:{nonce}:{ha2}")


def _select_charset(charset_param: str | None) -> str:
    """Choose the encoding for credential hashing per RFC 7616 §3.4.

    RFC 7616 defines exactly one valid ``charset`` value — ``UTF-8`` — which
    a server advertises to request that ``username`` and ``password`` be
    encoded as UTF-8 before hashing. When the directive is absent (or carries
    any other value), the legacy RFC 2617 default of ISO-8859-1 applies.

    Args:
        charset_param: The raw ``charset`` directive from the challenge, or
            ``None`` if the server did not send one. Matched case-insensitively
            against ``UTF-8``.

    Returns:
        The Python codec name to pass to ``str.encode`` — ``"utf-8"`` when the
        server advertised ``charset=UTF-8``, otherwise ``"iso-8859-1"``.
    """
    if charset_param is not None and charset_param.strip().upper() == "UTF-8":
        return "utf-8"
    return "iso-8859-1"


def _request_uri(url: Url) -> str:
    """Compute the ``uri`` parameter — path plus query, per RFC 7616 §3.4.6."""
    path = url.path or "/"
    if len(url.query):
        return f"{path}?{url.query.encode()}"
    return path


def _format_header(
    *,
    username: str,
    realm: str,
    nonce: str,
    uri: str,
    response: str,
    algorithm: str,
    cnonce: str,
    nc: str,
    qop: str | None,
    opaque: str | None,
) -> str:
    parts: list[str] = [
        f'username="{_quote(username)}"',
        f'realm="{_quote(realm)}"',
        f'nonce="{_quote(nonce)}"',
        f'uri="{_quote(uri)}"',
        f'response="{response}"',
        # ``algorithm`` is conventionally sent unquoted (RFC 7616 §3.4).
        f"algorithm={algorithm}",
    ]
    if qop is not None:
        # RFC 7616 §3.4: ``cnonce`` and ``nc`` are sent only alongside
        # ``qop``. A qop-less (RFC 2069) response must omit both.
        parts.append(f'cnonce="{_quote(cnonce)}"')
        # ``nc`` is conventionally unquoted (8 lowercase hex digits).
        parts.append(f"nc={nc}")
        parts.append(f"qop={qop}")
    if opaque is not None:
        parts.append(f'opaque="{_quote(opaque)}"')
    return "Digest " + ", ".join(parts)


def _quote(value: str) -> str:
    """Escape ``"`` and ``\\`` for inclusion in a quoted-string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "DigestAlgorithm",
    "DigestChallengeHandler",
]
