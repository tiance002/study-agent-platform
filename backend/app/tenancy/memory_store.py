"""进程内仓储适配器（开发用）。

⚠️ **降级声明（重要）**：本适配器在**应用层**强制租户过滤，
   不提供数据库级 RLS。生产必须替换为 PostgreSQL 适配器
   （独立非所有者角色 + 强制 RLS + 事务内 `SET LOCAL`），本版未实现。

   因此 **本版不具备多租户生产部署能力**，只用于验证边界逻辑与流程闭环。
   这一点写在这里而不是只写在 README，是为了让读代码的人无法忽略它。
"""

from __future__ import annotations

from copy import deepcopy

from app.core.errors import ErrorCode, deny
from app.tenancy.context import current


class InMemoryRepository:
    """把租户过滤放在唯一的读写路径上，业务代码无法绕过。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    # ------------------------------------------------------------------ 写

    def put(self, collection: str, record: dict) -> None:
        context = current()
        tenant_id = record.get("tenant_id")
        if not tenant_id:
            raise deny(
                ErrorCode.TENANT_CONTEXT_MISSING,
                f"集合 {collection} 的记录缺少 tenant_id；"
                f"所有项目级实体必须包含 tenant_id（04 号规格 §1）",
                collection=collection,
            )
        if tenant_id != context.tenant_id:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "写入的 tenant_id 与当前上下文不一致",
                expected=context.tenant_id,
                actual=tenant_id,
            )
        record_id = record.get("id")
        if not record_id:
            raise deny(ErrorCode.TENANT_CONTEXT_MISSING, "记录缺少 id")
        self._data.setdefault(collection, {})[record_id] = deepcopy(record)

    # ------------------------------------------------------------------ 读

    def get(self, collection: str, record_id: str) -> dict | None:
        context = current()
        record = self._data.get(collection, {}).get(record_id)
        if record is None:
            return None
        if record.get("tenant_id") != context.tenant_id:
            # 内部代码里出现跨租户 ID 一定是缺陷，因此明确报错而不是静默返回 None。
            # 对外部请求由 API 层统一转为 404，不暴露内容存在性。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "尝试读取其他租户的记录",
                collection=collection,
                record_id=record_id,
            )
        return deepcopy(record)

    def list_all(self, collection: str, *, project_scoped: bool = True) -> list[dict]:
        context = current()
        items = [
            deepcopy(record)
            for record in self._data.get(collection, {}).values()
            if record.get("tenant_id") == context.tenant_id
        ]
        if project_scoped:
            project_id = context.require_project()
            items = [r for r in items if r.get("learning_project_id") == project_id]
        return items

    # ------------------------------------------------------------------ 运维

    def collections(self) -> tuple[str, ...]:
        return tuple(sorted(self._data))

    def purge_all(self) -> None:
        """仅供测试使用。生产适配器**不得**提供此方法。"""
        self._data.clear()
