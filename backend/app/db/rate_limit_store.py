"""认证限流的 PostgreSQL 适配器。

计数由 0004 迁移的 `register_auth_attempt()`（SECURITY DEFINER）完成：
应用角色只有函数 EXECUTE 权限，直接读写 `auth_attempt_counters` 一律被拒。
这样未认证路径（没有租户上下文可设）也能原子计数，且不获得任何裸表访问。

窗口对齐在 SQL 内用 epoch 整除完成，与
`identity.rate_limit.window_start_epoch()` 是同一个公式。
"""

from __future__ import annotations

from datetime import datetime

from app.db.session import connect
from app.identity.rate_limit import (
    RateLimitDecision,
    window_start_epoch,
)


class PostgresRateLimiter:
    """固定窗口限流的数据库实现（跨 worker / 跨进程成立）。"""

    def __init__(self, *, limit: int, window_seconds: int, dsn: str | None = None) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("限流次数与窗口秒数都必须为正")
        self._limit = limit
        self._window = window_seconds
        self._dsn = dsn

    def register(self, key: str, *, now: datetime) -> RateLimitDecision:
        # 无租户上下文：函数是 SECURITY DEFINER，自己完成 upsert。
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT register_auth_attempt(%s, %s)",
                (key, self._window),
            ).fetchone()
            conn.commit()
        attempts = int(row[0]) if row is not None else 0
        retry_after = 0
        if attempts > self._limit:
            start = window_start_epoch(now, self._window)
            retry_after = self._window - (int(now.timestamp()) - start)
        return RateLimitDecision(
            allowed=attempts <= self._limit,
            attempts=attempts,
            limit=self._limit,
            window_seconds=self._window,
            retry_after_seconds=max(retry_after, 0),
        )
