# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance battery for ``UrlRedactor``.

Certifies the one default-deny URL redactor against every technical note in its
contract — userinfo stripping, query allow-listing, atomic multi-value
redaction, fragment scrubbing, component preservation, and total (never-raising)
parsing of malformed input — plus a fuzz check that ``redact`` cannot throw and
a "used-by-all" audit that the repr, logging, and tracing emitters all route
through this same redactor rather than reimplementing their own.
"""

from __future__ import annotations

import random
import string
from typing import Self

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.common.url import QueryParams
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.instrumentation import UrlRedactor
from dexpace.sdk.core.instrumentation.instrumentation_context import InstrumentationContext
from dexpace.sdk.core.instrumentation.noop import NOOP_INSTRUMENTATION_CONTEXT, NOOP_SPAN
from dexpace.sdk.core.instrumentation.span import Span
from dexpace.sdk.core.instrumentation.tracer import Tracer
from dexpace.sdk.core.instrumentation.tracing_scope import TracingScope
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import HttpLogDetailLevel, LoggingPolicy
from dexpace.sdk.core.pipeline.policies.tracing_policy import TracingPolicy, _set_request_attributes

_MALFORMED = "[malformed url]"


class TestUserinfo:
    """Userinfo (embedded credentials) is always redacted."""

    @pytest.mark.req("OBS-11", "XCUT-19")
    def test_strips_userinfo_from_string(self) -> None:
        redacted = UrlRedactor().redact("https://alice:s3cr3t@api.example.com/path")
        assert "alice" not in redacted
        assert "s3cr3t" not in redacted
        assert redacted == "https://api.example.com/path"

    def test_strips_userinfo_from_parsed_url(self) -> None:
        parsed = Url(scheme="https", host="api.example.com", userinfo="alice:s3cr3t")
        redacted = UrlRedactor().redact(parsed)
        assert "alice" not in redacted
        assert "s3cr3t" not in redacted
        assert "api.example.com" in redacted

    @pytest.mark.req("OBS-11")
    def test_strips_userinfo_with_at_in_password(self) -> None:
        # The last '@' delimits userinfo, so an '@' inside the password does not
        # leave credential fragments behind.
        redacted = UrlRedactor().redact("https://user:p@ss@api.example.com/path")
        assert redacted == "https://api.example.com/path"


class TestHeaderValueRedaction:
    """``redact_header_value`` handles both absolute and relative URL values."""

    @pytest.mark.req("OBS-16")
    def test_absolute_value_redacted_like_a_request_url(self) -> None:
        # A parseable absolute value is redacted exactly like a request URL:
        # userinfo stripped, non-allowlisted query collapsed.
        out = UrlRedactor().redact_header_value(
            "https://alice:pw@api.example.com/cb?code=SEKRET&api-version=1.0"
        )
        assert "alice" not in out
        assert "SEKRET" not in out
        assert "REDACTED=REDACTED" in out
        assert "api-version=1.0" in out
        assert out.startswith("https://api.example.com/cb?")

    @pytest.mark.req("OBS-16")
    def test_relative_value_with_query_keeps_path_and_marks(self) -> None:
        # A relative value keeps its path and drops the query, since the query
        # can carry an OAuth code / pre-signed signature.
        out = UrlRedactor().redact_header_value("/callback?code=SEKRET&state=xyz")
        assert out == "/callback?***"
        assert "SEKRET" not in out

    @pytest.mark.req("OBS-16")
    def test_relative_value_with_fragment_only_keeps_path_and_marks(self) -> None:
        # An implicit-flow token can ride in the fragment, so a fragment alone
        # still triggers the drop-and-mark behaviour.
        out = UrlRedactor().redact_header_value("/callback#access_token=SEKRET")
        assert out == "/callback?***"
        assert "SEKRET" not in out

    @pytest.mark.req("OBS-16")
    def test_relative_value_without_query_or_fragment_is_verbatim(self) -> None:
        assert UrlRedactor().redact_header_value("/callback/next") == "/callback/next"

    @pytest.mark.req("OBS-16")
    def test_unparseable_value_with_query_is_dropped(self) -> None:
        # ``?`` inside a fragment must not be mistaken for a query separator.
        assert UrlRedactor().redact_header_value("mailto:x#a?b") == "mailto:x?***"
        assert UrlRedactor().redact_header_value("garbage value") == "garbage value"

    @pytest.mark.req("OBS-16")
    def test_never_raises_on_adversarial_values(self) -> None:
        for value in ("", "#", "?", "://", "http://", "/a?b#c?d", "\x00\x01"):
            assert isinstance(UrlRedactor().redact_header_value(value), str)


class TestQueryParameters:
    """Query values are redacted unless explicitly allow-listed."""

    @pytest.mark.req("OBS-12", "XCUT-19")
    def test_allowlisted_query_kept_others_redacted(self) -> None:
        out = UrlRedactor().redact("https://api.example.com/v1?api-version=1.0&token=hunter2")
        assert "api-version=1.0" in out
        # Non-allowlisted params collapse to REDACTED=REDACTED so the parameter
        # name ("token") never leaks either.
        assert "REDACTED=REDACTED" in out
        assert "token" not in out
        assert "hunter2" not in out

    def test_custom_allowlist(self) -> None:
        out = UrlRedactor(allowed_query_keys={"plain"}).redact(
            "https://example.com/?plain=ok&api-version=1.0"
        )
        assert "plain=ok" in out
        # api-version is not in the custom allow-list; key and value are redacted.
        assert "REDACTED=REDACTED" in out
        assert "api-version" not in out

    def test_bare_token_query_redacts_key_and_value(self) -> None:
        # A bare token with no '=' parses as a key with an empty value; without
        # key redaction the secret would survive verbatim as the parameter name.
        token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
        out = UrlRedactor().redact(f"https://api.example.com/v1?{token}")
        assert token not in out
        assert "REDACTED=REDACTED" in out

    def test_empty_query_introduces_no_separator(self) -> None:
        out = UrlRedactor().redact("https://api.example.com/v1?")
        assert out == "https://api.example.com/v1"


class TestMultiValueAtomicRedaction:
    """A multi-value parameter is redacted all-or-nothing, per key."""

    @pytest.mark.req("OBS-12")
    def test_non_allowlisted_multivalue_all_redacted(self) -> None:
        out = UrlRedactor(allowed_query_keys=set()).redact("https://example.com/?a=1&a=2")
        assert out.count("REDACTED=REDACTED") == 2
        assert "a=" not in out
        assert "1" not in out and "2" not in out

    @pytest.mark.req("OBS-12")
    def test_allowlisted_multivalue_all_kept(self) -> None:
        # The allow-list decision is atomic at the key level: every value of an
        # allow-listed key survives, none are dropped or partially redacted.
        out = UrlRedactor(allowed_query_keys={"sort"}).redact(
            "https://example.com/?sort=asc&sort=desc&sig=secret"
        )
        assert "sort=asc" in out
        assert "sort=desc" in out
        assert "secret" not in out
        assert "REDACTED=REDACTED" in out


class TestFragment:
    """Fragment key=value pairs are scrubbed; plain fragments are preserved."""

    def test_key_value_fragment_scrubbed_by_default(self) -> None:
        out = UrlRedactor().redact("https://x/api?api-version=1#access_token=abc")
        assert "abc" not in out
        assert out.endswith("#REDACTED=REDACTED")

    @pytest.mark.req("OBS-13")
    def test_multi_segment_fragment_scrubs_only_pairs(self) -> None:
        out = UrlRedactor().redact("https://x/p#access_token=abc&state=xyz&plainpart")
        assert "abc" not in out
        assert "xyz" not in out
        # The plain, non-key/value segment survives verbatim.
        assert out == "https://x/p#REDACTED=REDACTED&REDACTED=REDACTED&plainpart"

    @pytest.mark.req("OBS-13")
    def test_plain_fragment_preserved_as_is(self) -> None:
        out = UrlRedactor().redact("https://x/page#section-heading")
        assert out == "https://x/page#section-heading"

    def test_fragment_preserved_when_redaction_disabled(self) -> None:
        out = UrlRedactor(redact_fragment=False).redact("https://x/api?api-version=1#token=abc")
        assert "token=abc" in out


class TestComponentPreservation:
    """Scheme, host, port, and path survive redaction unchanged."""

    @pytest.mark.req("OBS-14")
    def test_scheme_host_port_path_preserved(self) -> None:
        out = UrlRedactor().redact("https://api.example.com:8443/v1/users/42")
        assert out == "https://api.example.com:8443/v1/users/42"

    def test_path_preserved_by_default(self) -> None:
        out = UrlRedactor().redact("https://x/users/123/secret/abc")
        assert "/users/123/secret/abc" in out

    def test_path_redacted_when_enabled(self) -> None:
        out = UrlRedactor(redact_path=True).redact("https://x/users/123/secret/abc")
        assert "123" not in out
        assert "abc" not in out
        assert out == "https://x/REDACTED"


class TestNoSpuriousQuerySeparator:
    """A literal '?' inside a fragment must not be re-split as a query."""

    @pytest.mark.req("OBS-14")
    def test_question_mark_in_fragment_stays_in_fragment(self) -> None:
        out = UrlRedactor().redact("https://x/path#section?still-fragment")
        # Exactly one '?', and it sits after the '#' (i.e. inside the fragment).
        assert out == "https://x/path#section?still-fragment"
        assert out.count("?") == 1
        assert out.index("#") < out.index("?")

    @pytest.mark.req("OBS-14")
    def test_real_query_plus_question_mark_in_fragment(self) -> None:
        out = UrlRedactor(allowed_query_keys={"a"}).redact("https://x/p?a=1#frag?x")
        # The query '?' and the fragment '?' both survive; no third is injected.
        assert out == "https://x/p?a=1#frag?x"


class TestMalformedInput:
    """Malformed input redacts to a literal placeholder and never throws."""

    @pytest.mark.req("XCUT-20")
    def test_not_a_url(self) -> None:
        assert UrlRedactor().redact("not a url") == _MALFORMED

    def test_missing_scheme_secret_does_not_leak(self) -> None:
        secret = "Bearer sk-live-abc123def456"
        out = UrlRedactor().redact(f"://{secret}")
        assert secret not in out
        assert out == _MALFORMED

    def test_invalid_port(self) -> None:
        assert UrlRedactor().redact("http://host:notaport/path") == _MALFORMED

    def test_out_of_range_port(self) -> None:
        assert UrlRedactor().redact("http://host:99999/path") == _MALFORMED

    def test_unterminated_ipv6(self) -> None:
        assert UrlRedactor().redact("http://[bad") == _MALFORMED

    def test_empty_string(self) -> None:
        assert UrlRedactor().redact("") == _MALFORMED

    def test_scheme_only(self) -> None:
        assert UrlRedactor().redact("https://") == _MALFORMED

    def test_non_ascii_digit_port(self) -> None:
        # Superscript '2' is str.isdigit() but not int()-parseable; the redactor
        # must reject it without raising rather than trusting isdigit alone.
        assert UrlRedactor().redact("http://host:²/path") == _MALFORMED


class TestParsedUrlInput:
    """A pre-parsed ``Url`` is redacted directly, without re-parsing."""

    def test_accepts_parsed_url(self) -> None:
        parsed = Url.parse("https://api.example.com/v1?secret=value")
        out = UrlRedactor().redact(parsed)
        assert "REDACTED=REDACTED" in out
        assert "secret" not in out
        assert "value" not in out

    def test_parsed_url_full_surface(self) -> None:
        parsed = Url(
            scheme="https",
            host="api.example.com",
            port=8443,
            path="/v1",
            query=QueryParams([("token", "s3cr3t")]),
            fragment="access_token=abc",
            userinfo="alice:pw",
        )
        out = UrlRedactor().redact(parsed)
        for leaked in ("alice", "pw", "s3cr3t", "abc", "token"):
            assert leaked not in out
        assert "https://api.example.com:8443/v1" in out


class TestNeverThrows:
    """``redact`` is total: no input string makes it raise."""

    _ADVERSARIAL = (
        "",
        "://",
        "http://",
        "https://:@",
        "http://[",
        "http://]",
        "http://[::1",
        "http://host:",
        "http://host:-1/x",
        "http://host:99999999999999999999/x",
        "%%%",
        "?#",
        "#?",
        "https://h/p?%zz=%gg#%",
        "https://h/p?a=1;b=2",
        "\x00\x01\x02",
        "https://\U0001f600/\U0001f4a9?☃=❤#⚡",
        " https://host /p ",
        "ht tp://host/p",
        "://secret-token",
        "a" * 5000,
    )

    @pytest.mark.req("OBS-15", "XCUT-20")
    def test_adversarial_corpus_never_raises(self) -> None:
        redactor = UrlRedactor()
        for raw in self._ADVERSARIAL:
            result = redactor.redact(raw)
            assert isinstance(result, str)

    def test_random_fuzz_never_raises(self) -> None:
        # A stdlib fuzz stands in for a hypothesis property test: hypothesis is
        # not a dev dependency, so a seeded RNG drives a deterministic sweep of
        # arbitrary strings and asserts the redactor cannot throw on any of them.
        alphabet = string.ascii_letters + string.digits + ":/?#[]@!$&'()*+,;=%.-_~ \t\n\U0001f4a9"
        rng = random.Random(0xC0FFEE)
        configs = [
            UrlRedactor(),
            UrlRedactor(redact_path=True),
            UrlRedactor(redact_fragment=False),
            UrlRedactor(allowed_query_keys=set()),
        ]
        for _ in range(4000):
            raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            for redactor in configs:
                result = redactor.redact(raw)
                assert isinstance(result, str)


class _RecordingSpan(Span):
    """Minimal `Span` that records only the attributes set on it."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def context(self) -> InstrumentationContext:
        return NOOP_INSTRUMENTATION_CONTEXT

    def set_attribute(self, key: str, value: object) -> Self:
        self.attributes[key] = value
        return self

    def set_error(self, error_type: str) -> Self:
        return self

    def make_current(self) -> TracingScope:
        return NOOP_SPAN.make_current()

    def end(self, error: BaseException | None = None) -> None:
        return None


class _RecordingTracer(Tracer):
    """`Tracer` handing out one shared recording span."""

    def __init__(self, span: Span) -> None:
        self._span = span

    def start_span(self, name: str, parent: InstrumentationContext | None = None) -> Span:
        return self._span


class _StubClient(HttpClient):
    """Terminal client returning a fixed 200 response."""

    def execute(self, request: Request) -> Response:
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _RecordingLogger:
    """`ClientLogger`-shaped double that captures structured log fields."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def info(self, message: str, **fields: object) -> None:
        self.records.append({"message": message, **fields})

    def error(self, message: str, **fields: object) -> None:
        self.records.append({"message": message, **fields})


class TestUsedByAllEmitters:
    """Every URL emitter routes through the one shared ``UrlRedactor``."""

    _SECRET_URL = "https://alice:pw@api.example.com/v1?token=s3cr3t#access_token=abc"

    def _request(self) -> Request:
        return Request(method=Method.GET, url=Url.parse(self._SECRET_URL))

    def test_request_repr_redacts_url(self) -> None:
        rendered = repr(self._request())
        for leaked in ("alice", "pw", "s3cr3t", "abc"):
            assert leaked not in rendered
        assert "REDACTED" in rendered

    def test_response_repr_redacts_url(self) -> None:
        # Response has no URL of its own; it must inherit redaction via the
        # request repr rather than re-exposing the raw URL.
        response = Response(
            request=self._request(),
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
        )
        rendered = repr(response)
        for leaked in ("alice", "pw", "s3cr3t", "abc"):
            assert leaked not in rendered

    def test_tracing_url_full_attribute_redacted(self) -> None:
        span = _RecordingSpan()
        _set_request_attributes(span, self._request(), UrlRedactor())
        url_full = span.attributes["url.full"]
        assert isinstance(url_full, str)
        assert "s3cr3t" not in url_full
        assert "alice" not in url_full
        assert "REDACTED" in url_full

    def test_tracing_policy_wires_redactor_end_to_end(self) -> None:
        span = _RecordingSpan()
        policy = TracingPolicy(tracer=_RecordingTracer(span))
        with Pipeline(_StubClient(), policies=[policy]) as pipeline:
            pipeline.run(self._request(), DispatchContext(NOOP_INSTRUMENTATION_CONTEXT))
        url_full = span.attributes["url.full"]
        assert isinstance(url_full, str)
        assert "s3cr3t" not in url_full
        assert "REDACTED" in url_full

    def test_logging_policy_redacts_url(self) -> None:
        logger = _RecordingLogger()
        policy = LoggingPolicy(logger=logger, detail_level=HttpLogDetailLevel.HEADERS)  # type: ignore[arg-type]
        with Pipeline(_StubClient(), policies=[policy]) as pipeline:
            pipeline.run(self._request(), DispatchContext(NOOP_INSTRUMENTATION_CONTEXT))
        assert logger.records
        for record in logger.records:
            url = record.get("url.full")
            assert isinstance(url, str)
            assert "s3cr3t" not in url
            assert "alice" not in url
