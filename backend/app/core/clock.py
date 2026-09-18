"""可注入时钟与投影期时钟防护。

设计依据：01 号规格 §6 —— 投影算法禁止使用随机、当前时间、实时 ACL、
外部模型，以及未进入 `projection_input_set_hash` 的配置。

做法：投影期间读取系统时钟**直接抛错**，而不是靠人记得别写 `datetime.now()`。
这是把「文档约定」变成「运行时断言」的具体示例（00 号规格 §2 的要求）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol


class ClockViolation(RuntimeError):
    """在投影作用域内读取了系统当前时间。"""


_projection_active: ContextVar[bool] = ContextVar("projection_active", default=False)


def in_projection_scope() -> bool:
    """当前是否处于投影作用域。"""
    return _projection_active.get()


@contextmanager
def projection_scope() -> Iterator[None]:
    """进入投影作用域；离开后自动恢复。

    使用 ContextVar 而非全局变量，保证并发投影之间互不干扰。
    """
    token = _projection_active.set(True)
    try:
        yield
    finally:
        _projection_active.reset(token)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """系统时钟。在投影作用域内调用即抛错。"""

    def now(self) -> datetime:
        if in_projection_scope():
            raise ClockViolation(
                "投影过程禁止读取当前时间；freshness 应在展示层计算（01 号规格 §6）"
            )
        return datetime.now(timezone.utc)


class FixedClock:
    """固定时钟。用于测试与投影重放，保证同一输入必得同一结果。"""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, **delta) -> None:
        """推进时钟。参数与 `datetime.timedelta` 一致。"""
        self._instant = self._instant + timedelta(**delta)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """构造 UTC 时间的便捷函数，避免测试里到处写 timezone.utc。"""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
