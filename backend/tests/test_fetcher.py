"""Round 9 bounded response and redirect tests without opening a socket."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.knowledge.fetch_policy import FetchPolicy, FetchPolicyError
from app.knowledge.fetcher import FetchResult, fetch_url


def public_resolver(host: str, port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str]
    chunks: list[bytes]

    def read(self, size: int) -> bytes:
        del size
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def close(self) -> None:
        return None


class ScriptedTransport:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, target, timeout_seconds: float) -> FakeResponse:
        del timeout_seconds
        self.calls.append(target.url)
        return self.responses[target.url]


def test_reads_response_in_bounded_chunks_and_returns_metadata() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/guide": FakeResponse(
                status=200,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                chunks=[b"hello ", b"world"],
            )
        }
    )

    result = fetch_url(
        "https://example.com/guide",
        resolver=public_resolver,
        transport=transport,
    )

    assert isinstance(result, FetchResult)
    assert result.content == b"hello world"
    assert result.content_type == "text/plain; charset=utf-8"
    assert result.redirects == ()
    assert transport.calls == ["https://example.com/guide"]


def test_follows_relative_redirects_only_within_the_same_bounded_policy() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/start": FakeResponse(
                status=302,
                headers={"Location": "/final"},
                chunks=[],
            ),
            "https://example.com/final": FakeResponse(
                status=200,
                headers={},
                chunks=[b"done"],
            ),
        }
    )

    result = fetch_url(
        "https://example.com/start",
        resolver=public_resolver,
        transport=transport,
    )

    assert result.content == b"done"
    assert result.redirects == ("https://example.com/final",)
    assert transport.calls == ["https://example.com/start", "https://example.com/final"]


def test_redirect_to_private_address_is_rejected_before_second_request() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/start": FakeResponse(
                status=302,
                headers={"Location": "http://127.0.0.1/admin"},
                chunks=[],
            )
        }
    )

    with pytest.raises(FetchPolicyError) as caught:
        fetch_url("https://example.com/start", resolver=public_resolver, transport=transport)

    assert caught.value.code == "FETCH_PRIVATE_ADDRESS_DENIED"
    assert transport.calls == ["https://example.com/start"]


def test_https_redirect_cannot_downgrade_to_http() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/start": FakeResponse(
                status=302,
                headers={"Location": "http://example.com/final"},
                chunks=[],
            )
        }
    )

    with pytest.raises(FetchPolicyError) as caught:
        fetch_url("https://example.com/start", resolver=public_resolver, transport=transport)

    assert caught.value.code == "FETCH_DOWNGRADE_DENIED"
    assert transport.calls == ["https://example.com/start"]


def test_redirect_count_and_missing_location_are_bounded() -> None:
    too_many = ScriptedTransport(
        {
            "https://example.com/one": FakeResponse(302, {"Location": "/two"}, []),
            "https://example.com/two": FakeResponse(302, {"Location": "/three"}, []),
        }
    )
    with pytest.raises(FetchPolicyError) as caught:
        fetch_url(
            "https://example.com/one",
            policy=FetchPolicy(max_redirects=1),
            resolver=public_resolver,
            transport=too_many,
        )
    assert caught.value.code == "FETCH_REDIRECT_LIMIT"

    missing = ScriptedTransport(
        {"https://example.com/start": FakeResponse(302, {}, [])}
    )
    with pytest.raises(FetchPolicyError) as caught:
        fetch_url("https://example.com/start", resolver=public_resolver, transport=missing)
    assert caught.value.code == "FETCH_RESPONSE_INVALID"


def test_declared_and_streamed_payload_limits_are_both_enforced() -> None:
    declared = ScriptedTransport(
        {
            "https://example.com/large": FakeResponse(
                200,
                {"Content-Length": "5"},
                [b"never-read"],
            )
        }
    )
    with pytest.raises(FetchPolicyError) as caught:
        fetch_url(
            "https://example.com/large",
            policy=FetchPolicy(max_bytes=4),
            resolver=public_resolver,
            transport=declared,
        )
    assert caught.value.code == "FETCH_PAYLOAD_TOO_LARGE"

    streamed = ScriptedTransport(
        {"https://example.com/stream": FakeResponse(200, {}, [b"123", b"45"])}
    )
    with pytest.raises(FetchPolicyError) as caught:
        fetch_url(
            "https://example.com/stream",
            policy=FetchPolicy(max_bytes=4),
            resolver=public_resolver,
            transport=streamed,
        )
    assert caught.value.code == "FETCH_PAYLOAD_TOO_LARGE"


def test_retryable_http_and_transport_failures_are_classified() -> None:
    server_error = ScriptedTransport(
        {"https://example.com/retry": FakeResponse(503, {}, [b"busy"])}
    )
    with pytest.raises(FetchPolicyError) as caught:
        fetch_url("https://example.com/retry", resolver=public_resolver, transport=server_error)
    assert caught.value.code == "FETCH_HTTP_ERROR"
    assert caught.value.retryable is True

    def failing_transport(target, timeout_seconds: float):
        del target, timeout_seconds
        raise TimeoutError("test timeout")

    with pytest.raises(FetchPolicyError) as caught:
        fetch_url("https://example.com/timeout", resolver=public_resolver, transport=failing_transport)
    assert caught.value.code == "FETCH_TIMEOUT"
    assert caught.value.retryable is True


def test_total_deadline_is_checked_between_stream_reads() -> None:
    transport = ScriptedTransport(
        {"https://example.com/slow": FakeResponse(200, {}, [b"late"])}
    )
    times = iter((0.0, 0.0, 2.0))

    with pytest.raises(FetchPolicyError) as caught:
        fetch_url(
            "https://example.com/slow",
            policy=FetchPolicy(total_timeout_seconds=1.0),
            resolver=public_resolver,
            transport=transport,
            clock=lambda: next(times),
        )

    assert caught.value.code == "FETCH_TIMEOUT"
    assert caught.value.retryable is True
