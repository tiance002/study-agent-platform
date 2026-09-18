"""存储端口定义。

设计依据：06 号规格 §2 层间通信原语表 —— L3/L4 → L5 走事务，适配器可替换。

**本版只提供进程内适配器。** PostgreSQL + RLS 适配器未实现（见 ADR 与 README
的「未实现项」一节）。端口定义在此，是为了让替换点唯一且显式。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RecordRepository(Protocol):
    """最小仓储端口。所有方法都隐含要求租户上下文。

    故意不提供 `list_everything()` 这类方法：一旦存在，就一定会被误用。
    """

    def put(self, collection: str, record: dict) -> None: ...

    def get(self, collection: str, record_id: str) -> dict | None: ...

    def list_all(self, collection: str, *, project_scoped: bool = True) -> list[dict]: ...
