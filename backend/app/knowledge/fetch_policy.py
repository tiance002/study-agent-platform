"""安全的外部资料获取目标策略。

This module deliberately contains no HTTP client or database code.  A fetcher
must validate the original URL and every DNS result before opening a socket.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, NoReturn
from urllib.parse import SplitResult, urlsplit, urlunsplit


class FetchPolicyError(ValueError):
    """A stable, non-sensitive reason why an acquisition target was rejected."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """Independent bounds for one external acquisition attempt."""

    allowed_hosts: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (80, 443)
    max_bytes: int = 4 * 1024 * 1024
    max_redirects: int = 3
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    total_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if any(not isinstance(host, str) or not host.strip() for host in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain non-empty strings")
        if any(not isinstance(port, int) or not 1 <= port <= 65535 for port in self.allowed_ports):
            raise ValueError("allowed_ports must contain valid TCP ports")
        if self.max_bytes <= 0 or self.max_redirects < 0:
            raise ValueError("max_bytes must be positive and max_redirects cannot be negative")
        if any(
            value <= 0
            for value in (
                self.connect_timeout_seconds,
                self.read_timeout_seconds,
                self.total_timeout_seconds,
            )
        ):
            raise ValueError("fetch timeouts must be positive")


@dataclass(frozen=True, slots=True)
class FetchTarget:
    """A canonical URL whose resolved addresses passed the target policy."""

    url: str
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]


Resolver = Callable[[str, int], Iterable[str]]


def _raise(code: str, message: str, *, retryable: bool = False) -> NoReturn:
    raise FetchPolicyError(code, message, retryable=retryable)


def _canonical_hostname(hostname: str) -> str:
    try:
        canonical = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FetchPolicyError("FETCH_HOST_INVALID", "目标主机名无效") from exc
    if not canonical:
        _raise("FETCH_HOST_INVALID", "目标主机名不能为空")
    return canonical


def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchPolicyError("FETCH_DNS_FAILED", "目标主机解析失败", retryable=True) from exc

    addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
    if not addresses:
        _raise("FETCH_DNS_FAILED", "目标主机没有可用地址", retryable=True)
    return addresses


def _reject_non_global(address_text: str) -> None:
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError as exc:
        raise FetchPolicyError("FETCH_DNS_FAILED", "目标主机解析结果无效", retryable=True) from exc
    if not address.is_global:
        _raise("FETCH_PRIVATE_ADDRESS_DENIED", "目标地址不是公网地址")


def _canonical_url(parsed: SplitResult, hostname: str, port: int) -> str:
    host_for_url = hostname
    if ":" in host_for_url:
        host_for_url = f"[{host_for_url}]"
    default_port = 80 if parsed.scheme == "http" else 443
    netloc = host_for_url if port == default_port else f"{host_for_url}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def validate_fetch_target(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    resolver: Resolver | None = None,
) -> FetchTarget:
    """Validate and canonicalize one URL before any network connection.

    The resolver is injectable so policy tests can be deterministic and can
    prove that every address returned by DNS is checked.
    """

    effective_policy = policy or FetchPolicy()
    if not isinstance(url, str) or not url.strip() or any(char in url for char in "\r\n\t"):
        _raise("FETCH_URL_INVALID", "目标 URL 无效")

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname_from_url = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        _raise("FETCH_URL_INVALID", "目标 URL 无效")
        raise AssertionError("unreachable") from exc

    if scheme not in {"http", "https"}:
        _raise("FETCH_SCHEME_DENIED", "只允许 HTTP(S) 资料来源")
    if parsed.username is not None or parsed.password is not None:
        _raise("FETCH_CREDENTIALS_DENIED", "资料来源 URL 不得包含凭据")
    if hostname_from_url is None:
        _raise("FETCH_HOST_INVALID", "目标主机名不能为空")

    hostname = _canonical_hostname(hostname_from_url)
    port = explicit_port or (80 if scheme == "http" else 443)
    if port not in effective_policy.allowed_ports:
        _raise("FETCH_PORT_DENIED", "目标端口不在允许范围内")

    if hostname == "localhost" or hostname.endswith(".localhost"):
        _raise("FETCH_PRIVATE_ADDRESS_DENIED", "目标地址不是公网地址")
    allowlist = {_canonical_hostname(host) for host in effective_policy.allowed_hosts}
    if allowlist and hostname not in allowlist:
        _raise("FETCH_HOST_NOT_ALLOWED", "目标主机不在来源白名单中")

    try:
        direct_address = ipaddress.ip_address(hostname)
    except ValueError:
        direct_address = None

    resolved_ips: tuple[str, ...]
    if direct_address is not None:
        if not direct_address.is_global:
            _raise("FETCH_PRIVATE_ADDRESS_DENIED", "目标地址不是公网地址")
        resolved_ips = (str(direct_address),)
    else:
        effective_resolver = resolver or _default_resolver
        try:
            resolved_ips = tuple(dict.fromkeys(str(item) for item in effective_resolver(hostname, port)))
        except FetchPolicyError:
            raise
        except OSError as exc:
            raise FetchPolicyError("FETCH_DNS_FAILED", "目标主机解析失败", retryable=True) from exc
        if not resolved_ips:
            _raise("FETCH_DNS_FAILED", "目标主机没有可用地址", retryable=True)
        for address in resolved_ips:
            _reject_non_global(address)

    return FetchTarget(
        url=_canonical_url(parsed, hostname, port),
        hostname=hostname,
        port=port,
        resolved_ips=resolved_ips,
    )
