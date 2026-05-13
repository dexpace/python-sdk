"""Tests for credential types and the token cache."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dexpace.sdk.core.http.auth import (
    AccessTokenInfo,
    BasicAuthCredential,
    InMemoryTokenCache,
    KeyCredential,
    NamedKeyCredential,
)


class TestAccessTokenInfo:
    def test_is_expired_in_past(self) -> None:
        token = AccessTokenInfo(token="t", expires_on=int(time.time()) - 10)
        assert token.is_expired()

    def test_not_expired_future(self) -> None:
        token = AccessTokenInfo(token="t", expires_on=int(time.time()) + 3600)
        assert not token.is_expired()

    def test_needs_refresh_within_leeway(self) -> None:
        token = AccessTokenInfo(token="t", expires_on=int(time.time()) + 60)
        assert token.needs_refresh(leeway=300)

    def test_needs_refresh_via_refresh_on(self) -> None:
        now = int(time.time())
        token = AccessTokenInfo(token="t", expires_on=now + 3600, refresh_on=now - 1)
        assert token.needs_refresh()

    def test_repr_redacts_token(self) -> None:
        token = AccessTokenInfo(token="hunter2", expires_on=0)
        assert "hunter2" not in repr(token)


class TestKeyCredential:
    def test_rejects_empty_key(self) -> None:
        with pytest.raises(TypeError):
            KeyCredential("")

    def test_repr_redacts(self) -> None:
        cred = KeyCredential("hunter2")
        assert "hunter2" not in repr(cred)

    def test_update(self) -> None:
        cred = KeyCredential("a")
        cred.update("b")
        assert cred.key == "b"

    def test_update_rejects_empty(self) -> None:
        cred = KeyCredential("a")
        with pytest.raises(ValueError):
            cred.update("")


class TestNamedKeyCredential:
    def test_round_trip(self) -> None:
        cred = NamedKeyCredential("name", "key")
        assert cred.name == "name"
        assert cred.key == "key"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            NamedKeyCredential("", "k")
        with pytest.raises(ValueError):
            NamedKeyCredential("n", "")

    def test_repr_redacts(self) -> None:
        cred = NamedKeyCredential("alice", "hunter2")
        assert "alice" not in repr(cred)
        assert "hunter2" not in repr(cred)


class TestBasicAuthCredential:
    def test_encoded(self) -> None:
        cred = BasicAuthCredential("user", "pass")
        # base64("user:pass") = "dXNlcjpwYXNz"
        assert cred.encoded == "dXNlcjpwYXNz"

    def test_repr_redacts(self) -> None:
        cred = BasicAuthCredential("alice", "hunter2")
        assert "alice" not in repr(cred)
        assert "hunter2" not in repr(cred)


class TestInMemoryTokenCache:
    def test_get_returns_none_for_missing(self) -> None:
        cache = InMemoryTokenCache()
        assert cache.get(["scope"]) is None

    def test_set_then_get(self) -> None:
        cache = InMemoryTokenCache()
        token = AccessTokenInfo(token="t", expires_on=0)
        cache.set(["a", "b"], token)
        assert cache.get(["a", "b"]) is token
        # Order-independent lookup.
        assert cache.get(["b", "a"]) is token

    def test_audience_separates_entries(self) -> None:
        cache = InMemoryTokenCache()
        t1 = AccessTokenInfo(token="1", expires_on=0)
        t2 = AccessTokenInfo(token="2", expires_on=0)
        cache.set(["s"], t1, audience="aud-a")
        cache.set(["s"], t2, audience="aud-b")
        assert cache.get(["s"], audience="aud-a") is t1
        assert cache.get(["s"], audience="aud-b") is t2

    def test_clear(self) -> None:
        cache = InMemoryTokenCache()
        cache.set(["s"], AccessTokenInfo(token="t", expires_on=0))
        cache.clear()
        assert cache.get(["s"]) is None

    def test_concurrent_get_with_writes(self) -> None:
        """``get`` must be safe under concurrent ``set`` calls.

        The lock guarantees this on free-threaded CPython (PEP 703) and
        non-CPython runtimes; under the GIL this test is mostly a
        correctness demonstration.
        """
        cache = InMemoryTokenCache()
        token = AccessTokenInfo(token="t", expires_on=0)
        cache.set(["s"], token)

        stop = threading.Event()

        def writer() -> None:
            i = 0
            while not stop.is_set():
                cache.set([f"s{i % 8}"], AccessTokenInfo(token=str(i), expires_on=0))
                i += 1

        def reader() -> list[AccessTokenInfo | None]:
            seen: list[AccessTokenInfo | None] = []
            for _ in range(500):
                seen.append(cache.get(["s"]))
            return seen

        with ThreadPoolExecutor(max_workers=6) as pool:
            writers = [pool.submit(writer) for _ in range(2)]
            readers = [pool.submit(reader) for _ in range(4)]
            try:
                results = [f.result(timeout=5) for f in readers]
            finally:
                stop.set()
                for f in writers:
                    f.result(timeout=5)

        for seen in results:
            assert all(entry is token for entry in seen)
