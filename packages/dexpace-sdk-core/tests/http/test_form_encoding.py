# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Corpus vectors for the strict ``x-www-form-urlencoded`` codec used by
``RequestBody.from_form`` / ``AsyncRequestBody.from_form``.

The codec must percent-encode strictly (space -> ``%20``, never ``+``) so a
literal ``+`` and a space are never conflated, and every reserved delimiter is
escaped.
"""

from __future__ import annotations

import asyncio

import pytest

from dexpace.sdk.core.http.common import common_media_types
from dexpace.sdk.core.http.request import AsyncRequestBody, RequestBody


def _drain(body: RequestBody) -> bytes:
    return b"".join(body.iter_bytes())


def _adrain(body: AsyncRequestBody) -> bytes:
    async def run() -> bytes:
        return b"".join([chunk async for chunk in body.aiter_bytes()])

    return asyncio.run(run())


# One field per case so the expected encoding is unambiguous.
_CORPUS: list[tuple[str, str, bytes]] = [
    ("space encoded as %20", "k", b"a b"),  # -> a%20b  (never a+b)
    ("literal plus escaped", "k", b"a+b"),  # -> a%2Bb
    ("ampersand escaped", "k", b"a&b"),
    ("equals escaped", "k", b"a=b"),
    ("slash escaped", "k", b"a/b"),
    ("question mark escaped", "k", b"a?b"),
    ("hash escaped", "k", b"a#b"),
    ("percent escaped", "k", b"100%"),
    ("unicode via utf-8", "k", b"na\xc3\xafve"),  # naïve
]

_EXPECTED = {
    b"a b": b"k=a%20b",
    b"a+b": b"k=a%2Bb",
    b"a&b": b"k=a%26b",
    b"a=b": b"k=a%3Db",
    b"a/b": b"k=a%2Fb",
    b"a?b": b"k=a%3Fb",
    b"a#b": b"k=a%23b",
    b"100%": b"k=100%25",
    b"na\xc3\xafve": b"k=na%C3%AFve",
}


@pytest.mark.parametrize("label, key, raw", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_form_corpus_sync(label: str, key: str, raw: bytes) -> None:
    value = raw.decode("utf-8")
    body = RequestBody.from_form({key: value})
    out = _drain(body)
    assert out == _EXPECTED[raw], label
    # The plus-for-space trap must never appear: a space is %20, so a raw '+'
    # byte can only ever be an escaped literal, never present unescaped.
    if b" " in raw:
        assert b"+" not in out


@pytest.mark.parametrize("label, key, raw", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_form_corpus_async_matches_sync(label: str, key: str, raw: bytes) -> None:
    value = raw.decode("utf-8")
    sync = _drain(RequestBody.from_form({key: value}))
    asyncbody = _adrain(AsyncRequestBody.from_form({key: value}))
    assert sync == asyncbody == _EXPECTED[raw], label


def test_key_is_also_strictly_encoded() -> None:
    body = RequestBody.from_form({"a b&c": "1"})
    assert _drain(body) == b"a%20b%26c=1"


def test_media_type_is_form_urlencoded() -> None:
    body = RequestBody.from_form({"a": "1"})
    assert body.media_type() == common_media_types.APPLICATION_FORM_URLENCODED


def test_encoding_charset_changes_percent_bytes() -> None:
    fields = {"name": "é"}
    latin1 = _drain(RequestBody.from_form(fields, encoding="latin-1"))
    utf8 = _drain(RequestBody.from_form(fields, encoding="utf-8"))
    assert latin1 == b"name=%E9"
    assert utf8 == b"name=%C3%A9"


def test_multiple_fields_joined_with_ampersand() -> None:
    body = RequestBody.from_form({"a": "1", "b": "2"})
    assert _drain(body) == b"a=1&b=2"
