"""Bounded, policy-checked HTTP acquisition for the ingestion worker.

The worker calls :func:`fetch_url`; web request handlers must not call it.
Redirects are validated one hop at a time, and the default transport connects
to an address that already passed the DNS policy check rather than resolving
the hostname a second time.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from app.knowledge.fetch_policy import (
    FetchPolicy,
    FetchPolicyError,
    FetchTarget,
    validate_fetch_target,
)

READ_CHUNK_BYTES = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class FetchTimeouts:
    connect_timeout_seconds: float
    read_timeout_seconds: float


class FetchResponse(Protocol):
    status: int

    @property
    def headers(self) -> Mapping[str, str]:
        """Case-insensitive or normalized response headers."""

    def read(self, size: int) -> bytes:
        """Read at most size bytes, returning empty bytes at EOF."""

    def close(self) -> None:
        """Release the network response and its connection."""


Transport = Callable[[FetchTarget, FetchTimeouts], FetchResponse]


@dataclass(frozen=True, slots=True)
class FetchResult:
    final_target: FetchTarget
    content: bytes
    content_type: str
    redirects: tuple[str, ...]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: FetchTarget, timeout: float) -> None:
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._pinned_ip = target.resolved_ips[0]

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: FetchTarget, timeout: float) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(target.hostname, target.port, timeout=timeout, context=self._ssl_context)
        self._pinned_ip = target.resolved_ips[0]

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(self.sock, server_hostname=self.host)


class _ManagedHTTPResponse:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse, read_timeout: float):
        self._connection = connection
        self._response = response
        self.status = response.status
        self._headers = {str(key): str(value) for key, value in response.headers.items()}
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _request_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _default_transport(target: FetchTarget, timeouts: FetchTimeouts) -> FetchResponse:
    parsed = urlsplit(target.url)
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(target, timeouts.connect_timeout_seconds)
    else:
        connection = _PinnedHTTPConnection(target, timeouts.connect_timeout_seconds)
    try:
        connection.request(
            "GET",
            _request_path(target.url),
            headers={
                "Accept": "text/plain, text/markdown, text/html;q=0.5, application/xhtml+xml;q=0.5",
                "Connection": "close",
                "Host": target.hostname,
            },
        )
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    return _ManagedHTTPResponse(connection, response, timeouts.read_timeout_seconds)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value)
    return None


def _declared_length(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "Content-Length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError as exc:
        raise FetchPolicyError("FETCH_RESPONSE_INVALID", "来源响应长度无效") from exc
    if length < 0:
        raise FetchPolicyError("FETCH_RESPONSE_INVALID", "来源响应长度无效")
    return length


def _transport_error(exc: BaseException) -> FetchPolicyError:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return FetchPolicyError("FETCH_TIMEOUT", "来源请求超时", retryable=True)
    if isinstance(exc, OSError):
        return FetchPolicyError("FETCH_NETWORK_FAILED", "来源网络请求失败", retryable=True)
    return FetchPolicyError("FETCH_RESPONSE_INVALID", "来源响应无效")


def fetch_url(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    resolver: Callable[[str, int], tuple[str, ...]] | None = None,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FetchResult:
    """Fetch one bounded response, validating every redirect before opening it."""

    effective_policy = policy or FetchPolicy()
    effective_transport = transport or _default_transport
    deadline = clock() + effective_policy.total_timeout_seconds
    current_url = url
    redirects: list[str] = []
    previous_scheme = ""

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise FetchPolicyError("FETCH_TIMEOUT", "来源请求超时", retryable=True)
        target = validate_fetch_target(current_url, policy=effective_policy, resolver=resolver)
        current_scheme = urlsplit(target.url).scheme
        if previous_scheme == "https" and current_scheme == "http":
            raise FetchPolicyError("FETCH_DOWNGRADE_DENIED", "来源重定向不得降级到 HTTP")
        previous_scheme = current_scheme
        timeouts = FetchTimeouts(
            connect_timeout_seconds=min(effective_policy.connect_timeout_seconds, remaining),
            read_timeout_seconds=min(effective_policy.read_timeout_seconds, remaining),
        )

        try:
            response = effective_transport(target, timeouts)
        except FetchPolicyError:
            raise
        except Exception as exc:
            raise _transport_error(exc) from exc

        try:
            if response.status in REDIRECT_STATUSES:
                if len(redirects) >= effective_policy.max_redirects:
                    raise FetchPolicyError("FETCH_REDIRECT_LIMIT", "来源重定向次数超过上限")
                location = _header(response.headers, "Location")
                if not location:
                    raise FetchPolicyError("FETCH_RESPONSE_INVALID", "来源重定向缺少目标")
                next_url = urljoin(target.url, location)
                redirects.append(next_url)
                current_url = next_url
                continue

            if response.status < 200 or response.status >= 300:
                retryable = response.status == 408 or response.status == 429 or response.status >= 500
                raise FetchPolicyError("FETCH_HTTP_ERROR", "来源返回不可用的 HTTP 状态", retryable=retryable)

            declared_length = _declared_length(response.headers)
            if declared_length is not None and declared_length > effective_policy.max_bytes:
                raise FetchPolicyError("FETCH_PAYLOAD_TOO_LARGE", "来源响应超过大小上限")

            content = bytearray()
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise FetchPolicyError("FETCH_TIMEOUT", "来源请求超时", retryable=True)
                chunk = response.read(min(READ_CHUNK_BYTES, effective_policy.max_bytes - len(content) + 1))
                if not isinstance(chunk, bytes):
                    raise FetchPolicyError("FETCH_RESPONSE_INVALID", "来源响应内容无效")
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > effective_policy.max_bytes:
                    raise FetchPolicyError("FETCH_PAYLOAD_TOO_LARGE", "来源响应超过大小上限")

            return FetchResult(
                final_target=target,
                content=bytes(content),
                content_type=_header(response.headers, "Content-Type") or "",
                redirects=tuple(redirects),
            )
        except FetchPolicyError:
            raise
        except Exception as exc:
            raise _transport_error(exc) from exc
        finally:
            response.close()
