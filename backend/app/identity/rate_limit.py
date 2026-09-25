"""认证引导端点的限流（固定窗口）。

## 为什么需要它

注册与登录是**无需既有凭据即可调用**的端点：调用方可以反复尝试用户名/口令。
没有限流时，攻击者可以对账号口令做在线穷举，或用大量请求压垮 Argon2 路径。

## 语义

- 固定窗口，按**客户端键**（反向代理信任配置下取 X-Forwarded-For 首跳，
  否则取 TCP 对端）计数；
- 窗口起点按 epoch 整除对齐，内存实现与 PostgreSQL 实现（
  0004 迁移的 `register_auth_attempt()` definer 函数）用同一个公式，
  因此两种部署形态下计数行为一致、可由参数化契约测试互验；
- 每次调用都计数（包括被拒的那次），返回当前窗口第几次尝试与
  `Retry-After` 秒数；是否拒绝由 `attempts > limit` 判定。

固定窗口在窗口边界可能放过两倍流量，对"账号口令在线穷举"这个威胁足够；
精确滑动窗口的复杂度与成本不值得在这里承担。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RateLimitDecision:
    """一次限流判定结果。"""

    allowed: bool
    attempts: int
    limit: int
    window_seconds: int
    #: 被拒时距窗口重置的秒数；放行时为 0。
    retry_after_seconds: int

    @property
    def blocked(self) -> bool:
        return not self.allowed


class RateLimiter(Protocol):
    """注册一次尝试并给出判定。窗口对齐公式内存/PG 必须一致。"""

    def register(
        self,
        key: str,
        *,
        now: datetime,
        limit: int | None = None,
        window_seconds: int | None = None,
    ) -> RateLimitDecision: ...


def window_start_epoch(now: datetime, window_seconds: int) -> int:
    """当前窗口起点的 epoch 秒。PG 侧：

    ``to_timestamp(floor(extract(epoch from clock_timestamp()) / W) * W)``
    """
    return (int(now.timestamp()) // window_seconds) * window_seconds


class InMemoryRateLimiter:
    """进程内固定窗口限流（开发适配器；单进程成立）。"""

    def __init__(self, *, limit: int, window_seconds: int, capacity: int = 10_000) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("限流次数与窗口秒数都必须为正")
        if capacity <= 0:
            raise ValueError("限流桶容量必须为正")
        self._limit = limit
        self._window = window_seconds
        self._capacity = capacity
        self._counters: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        key: str,
        *,
        now: datetime,
        limit: int | None = None,
        window_seconds: int | None = None,
    ) -> RateLimitDecision:
        effective_limit = self._limit if limit is None else limit
        effective_window = self._window if window_seconds is None else window_seconds
        if effective_limit <= 0 or effective_window <= 0:
            raise ValueError("限流次数与窗口秒数都必须为正")
        start = window_start_epoch(now, effective_window)
        with self._lock:
            # TTL cleanup is bounded to the current counter map.  If a hostile
            # stream fills every bucket in one window, reject new keys instead
            # of growing memory without bound.
            if key not in self._counters and len(self._counters) >= self._capacity:
                self._counters = {
                    bucket: value
                    for bucket, value in self._counters.items()
                    if value[0] == start
                }
                if len(self._counters) >= self._capacity:
                    return RateLimitDecision(
                        allowed=False,
                        attempts=effective_limit + 1,
                        limit=effective_limit,
                        window_seconds=effective_window,
                        retry_after_seconds=effective_window,
                    )
            previous = self._counters.get(key)
            if previous is None or previous[0] != start:
                attempts = 1
            else:
                attempts = previous[1] + 1
            self._counters[key] = (start, attempts)

        retry_after = 0
        if attempts > effective_limit:
            retry_after = effective_window - (int(now.timestamp()) - start)
        return RateLimitDecision(
            allowed=attempts <= effective_limit,
            attempts=attempts,
            limit=effective_limit,
            window_seconds=effective_window,
            retry_after_seconds=max(retry_after, 0),
        )
