"""Bounded Argon2 work admission shared by registration and password login."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.errors import ErrorCode, PlatformError

T = TypeVar("T")


@dataclass
class Argon2WorkPool:
    max_concurrency: int = 2
    queue_limit: int = 16
    wait_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_concurrency <= 0 or self.queue_limit < 0 or self.wait_seconds <= 0:
            raise ValueError("invalid Argon2 pool limits")
        self._condition = threading.Condition()
        self._active = 0
        self._waiting_login = 0
        self._waiting_registration = 0

    def run(self, work: Callable[[], T], *, priority: str = "login") -> T:
        if priority not in {"login", "registration"}:
            priority = "login"
        with self._condition:
            if (
                self._active >= self.max_concurrency
                and self._waiting_login + self._waiting_registration >= self.queue_limit
            ):
                raise PlatformError(
                    ErrorCode.AUTH_POOL_SATURATED,
                    "认证计算资源暂时繁忙，请稍后重试",
                    retryable=True,
                    details={"retry_after_seconds": 1},
                )
            if priority == "login":
                self._waiting_login += 1
            else:
                self._waiting_registration += 1
            deadline = time.monotonic() + self.wait_seconds
        admitted = False
        try:
            with self._condition:
                while self._active >= self.max_concurrency or (
                    priority == "registration" and self._waiting_login > 0
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PlatformError(
                            ErrorCode.AUTH_POOL_SATURATED,
                            "认证计算资源暂时繁忙，请稍后重试",
                            retryable=True,
                            details={"retry_after_seconds": 1},
                        )
                    self._condition.wait(remaining)
                if priority == "login":
                    self._waiting_login -= 1
                else:
                    self._waiting_registration -= 1
                self._active += 1
                admitted = True
            return work()
        finally:
            with self._condition:
                if admitted:
                    self._active -= 1
                else:
                    # Admission timed out before decrementing its queue entry.
                    if priority == "login":
                        self._waiting_login -= 1
                    else:
                        self._waiting_registration -= 1
                self._condition.notify_all()
