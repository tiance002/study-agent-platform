"""Round 9 URL acquisition policy tests.

These tests are deliberately network-free.  A fetcher must prove its target
policy before it is allowed to open a socket.
"""

from __future__ import annotations

import pytest
from app.knowledge.fetch_policy import FetchPolicy, FetchPolicyError, validate_fetch_target


def public_resolver(host: str, port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def private_resolver(host: str, port: int) -> tuple[str, ...]:
    return ("127.0.0.1", "10.0.0.8")


def test_canonicalizes_public_http_target_and_drops_fragment() -> None:
    target = validate_fetch_target(
        "HTTPS://Example.COM:443/guide?a=1#private-fragment",
        resolver=public_resolver,
    )

    assert target.url == "https://example.com/guide?a=1"
    assert target.hostname == "example.com"
    assert target.port == 443


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:password@example.com/secret",
        "https://example.com:22/ssh",
        "https://example.com:8080/dev",
    ],
)
def test_rejects_unsafe_scheme_credentials_and_ports(url: str) -> None:
    with pytest.raises(FetchPolicyError) as caught:
        validate_fetch_target(url, resolver=public_resolver)

    assert caught.value.code in {
        "FETCH_SCHEME_DENIED",
        "FETCH_CREDENTIALS_DENIED",
        "FETCH_PORT_DENIED",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_rejects_local_and_metadata_targets(url: str) -> None:
    with pytest.raises(FetchPolicyError) as caught:
        validate_fetch_target(url, resolver=public_resolver)

    assert caught.value.code == "FETCH_PRIVATE_ADDRESS_DENIED"


def test_rechecks_every_resolved_address_for_private_ranges() -> None:
    with pytest.raises(FetchPolicyError) as caught:
        validate_fetch_target("https://example.com/", resolver=private_resolver)

    assert caught.value.code == "FETCH_PRIVATE_ADDRESS_DENIED"


def test_allowlist_is_exact_and_does_not_authorize_a_sibling_domain() -> None:
    policy = FetchPolicy(allowed_hosts=("docs.example.com",))

    validate_fetch_target(
        "https://docs.example.com/guide",
        policy=policy,
        resolver=public_resolver,
    )
    with pytest.raises(FetchPolicyError) as caught:
        validate_fetch_target(
            "https://not-docs.example.com/guide",
            policy=policy,
            resolver=public_resolver,
        )

    assert caught.value.code == "FETCH_HOST_NOT_ALLOWED"


def test_dns_failure_is_a_safe_retryable_policy_error() -> None:
    def failed_resolver(host: str, port: int) -> tuple[str, ...]:
        raise OSError("resolver unavailable")

    with pytest.raises(FetchPolicyError) as caught:
        validate_fetch_target("https://example.com/", resolver=failed_resolver)

    assert caught.value.code == "FETCH_DNS_FAILED"
    assert caught.value.retryable is True


def test_policy_keeps_independent_deadline_and_payload_limits() -> None:
    policy = FetchPolicy(
        max_bytes=2 * 1024 * 1024,
        max_redirects=3,
        connect_timeout_seconds=4.0,
        read_timeout_seconds=9.0,
        total_timeout_seconds=20.0,
    )

    assert policy.max_bytes == 2 * 1024 * 1024
    assert policy.max_redirects == 3
    assert policy.connect_timeout_seconds == 4.0
    assert policy.read_timeout_seconds == 9.0
    assert policy.total_timeout_seconds == 20.0
