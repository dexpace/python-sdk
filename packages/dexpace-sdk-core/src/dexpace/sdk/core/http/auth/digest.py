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
from typing import Final

from ..common.url import Url
from ..request.method import Method
from .challenge import AuthenticateChallenge

# Type alias for the algorithm parameter strings recognised on the wire.
DigestAlgorithm = str

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
            first preference matched against an offered challenge wins.
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
        algorithm = selected.parameters.get("algorithm", "MD5")
        algo_key = algorithm.upper()
        hasher = _HASHERS.get(algo_key)
        if hasher is None:
            return None
        realm = selected.parameters.get("realm", "")
        nonce = selected.parameters.get("nonce", "")
        opaque = selected.parameters.get("opaque")
        qop = self._pick_qop(selected.parameters.get("qop"))
        if qop is None and "qop" in selected.parameters:
            # The server advertised qop but did not include ``auth``: we
            # only implement auth, so we cannot satisfy this challenge.
            return None
        nc = self._next_nc()
        cnonce = self._cnonce_factory()
        uri = _request_uri(url)
        response = self._compute_response(
            hasher=hasher,
            algorithm=algo_key,
            method=str(method),
            uri=uri,
            realm=realm,
            nonce=nonce,
            nc=nc,
            cnonce=cnonce,
            qop=qop,
        )
        header_value = _format_header(
            username=self._username,
            realm=realm,
            nonce=nonce,
            uri=uri,
            response=response,
            algorithm=algorithm,
            cnonce=cnonce,
            nc=nc,
            qop=qop,
            opaque=opaque,
        )
        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        return header_name, header_value

    def _select(self, challenges: list[AuthenticateChallenge]) -> AuthenticateChallenge | None:
        """Return the best Digest challenge per ``preferred_algorithms``."""
        digest_challenges = [c for c in challenges if c.scheme.casefold() == "digest"]
        if not digest_challenges:
            return None
        # First, match by preference order.
        for preferred in self._preferred:
            target = preferred.upper()
            for challenge in digest_challenges:
                algo = challenge.parameters.get("algorithm", "MD5").upper()
                if algo == target and algo in _HASHERS:
                    return challenge
        # Fallback: take the first challenge whose algorithm we recognise.
        for challenge in digest_challenges:
            algo = challenge.parameters.get("algorithm", "MD5").upper()
            if algo in _HASHERS:
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
    ) -> str:
        def h(data: str) -> str:
            return hasher(data.encode("utf-8")).hexdigest()

        ha1 = h(f"{self._username}:{realm}:{self._password}")
        if algorithm.endswith("-SESS"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        if qop == "auth":
            return h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        # No qop (legacy RFC 2069): response = H(HA1:nonce:HA2)
        return h(f"{ha1}:{nonce}:{ha2}")


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
        f'cnonce="{_quote(cnonce)}"',
        # ``nc`` is conventionally unquoted (8 lowercase hex digits).
        f"nc={nc}",
    ]
    if qop is not None:
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
