"""认证引导端点的限流（固定窗口）。

## 为什么需要它

邀请兑换是**唯一的未认证写端点**：调用方不需要任何凭据即可反复尝试令牌。
没有限流时，攻击者可以对邀请令牌做在线穷举，或用大量请求淹没兑换路径。

## 语义

- 固定窗口，按**客户端键**（反向代理信任配置下取 X-Forwarded-For 首跳，
  否则取 TCP 对端）计数；
- 窗口起点按 epoch 整除对齐，内存实现与 PostgreSQL 实现（
  0004 迁移的 `register_auth_attempt()` definer 函数）用同一个公式，
  因此两种部署形态下计数行为一致、可由参数化契约测试互验；
- 每次调用都计数（包括被拒的那次），返回当前窗口第几次尝试与
  `Retry-After` 秒数；是否拒绝由 `attempts > limit` 判定。

固定窗口在窗口边界可能放过两倍流量，对"邀请令牌在线穷举"这个威胁足够；
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

    def register(self, key: str, *, now: datetime) -> RateLimitDecision: ...


def window_start_epoch(now: datetime, window_seconds: int) -> int:
    """当前窗口起点的 epoch 秒。PG 侧：

    ``to_timestamp(floor(extract(epoch from clock_timestamp()) / W) * W)``
    """
    return (int(now.timestamp()) // window_seconds) * window_seconds


class InMemoryRateLimiter:
    """进程内固定窗口限流（开发适配器；单进程成立）。"""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("限流次数与窗口秒数都必须为正")
        self._limit = limit
        self._window = window_seconds
        self._counters: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def register(self, key: str, *, now: datetime) -> RateLimitDecision:
        start = window_start_epoch(now, self._window)
        with self._lock:
            previous = self._counters.get(key)
            if previous is None or previous[0] != start:
                attempts = 1
            else:
                attempts = previous[1] + 1
            self._counters[key] = (start, attempts)

        retry_after = 0
        if attempts > self._limit:
            retry_after = self._window - (int(now.timestamp()) - start)
        return RateLimitDecision(
            allowed=attempts <= self._limit,
            attempts=attempts,
            limit=self._limit,
            window_seconds=self._window,
            retry_after_seconds=max(retry_after, 0),
        )
