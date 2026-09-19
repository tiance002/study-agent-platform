"""任务状态流转的唯一判定出口。

不变量 #5 的应用：同一语义不得有两个判定出口 —— 内存适配器、PostgreSQL
适配器、API 校验层都必须调用这里的同一个函数，而不是各自持一份迁移表
（三份表迟早漂移出"内存允许、数据库拒绝"的分歧）。

## MVP 的迁移图

    pending ──→ in_progress ──→ done
       │             │
       └──→ skipped ←┘

done 与 skipped 是终态。没有回退：回到 pending 等于抹掉"已经动工"的事实，
而诚实记录"做了又停下"（skipped）比假装没发生更符合证据链的立场。

## 迁移合法性与并发是两件事

- **合法性**是纯函数：`(当前状态, 目标状态) → 是否允许`，不看存储；
- **并发**是实现的职责：条件更新（`WHERE status = expected`）的命中行数判定。
  两者混在一起（先读再判再写）就是 check-then-act。
"""

from __future__ import annotations

from app.core.errors import ErrorCode, deny
from app.product.models import TaskStatus

#: 合法迁移表。唯一的真相源。
LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.SKIPPED}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DONE, TaskStatus.SKIPPED}),
    # 终态：没有出边。
    TaskStatus.DONE: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
}


def is_legal(current: TaskStatus, next_status: TaskStatus) -> bool:
    return next_status in LEGAL_TRANSITIONS[current]


def assert_transition_legal(current: TaskStatus, next_status: TaskStatus) -> None:
    """非法迁移的统一拒绝出口（同码同话术）。"""
    if not is_legal(current, next_status):
        raise deny(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"任务不能从 {current.value} 转到 {next_status.value}；"
            f"允许的去向：{sorted(s.value for s in LEGAL_TRANSITIONS[current])}",
            current_status=current.value,
            next_status=next_status.value,
        )
