"""确认记录的 PostgreSQL 适配器。

单次消费的原子占用，核心是**一条语句** —— 这是审查修复的正式落地：

```sql
UPDATE confirmations
   SET consumed_at = %s
 WHERE confirmation_id = %s
   AND consumed_at IS NULL
RETURNING ...
```

判断「是否已消费」与「标记已消费」是同一条语句：并发下第二个事务的
UPDATE 会等第一个的行锁；拿到锁后在**新版本行**上重新评估 WHERE
（Read Committed 的 EvalPlanQual 机制），条件不再成立 → 零行 → 拒绝。

这条原子性由数据库保证，跨进程、跨 worker 都成立 ——
这正是进程内锁给不了的，也是审查指出的方向。

## 绑定校验与预算上界

校验复用 `validate_binding` / `assert_within_ceiling`（模块级函数），
与内存适配器共用 —— 两种实现的语义必须一致，否则迟早漂移成
「内存版拦截、数据库版放行」。

预算校验刻意放在**占用之前**：超界被拒时确认不能被消耗掉，
否则用户要为一个没执行的动作重新确认一次。

## RLS 与这个存储的关系

所有读写都发生在 `tenant_transaction()` 里（带 `app.tenant_id` /
`app.project_id` 上下文），RLS 在数据库层过滤行。也就是说，
「用 A 项目的上下文读 B 项目的确认」根本不会看到那行 —— 表现为
「不存在或已失效」，而不是一条错误消息里带着 B 项目的存在性。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id
from app.db.session import tenant_transaction
from app.execution.confirmation import (
    DEFAULT_CONFIRMATION_TTL,
    BudgetCeiling,
    ConfirmationRecord,
    assert_within_ceiling,
    validate_binding,
)

_SELECT_COLUMNS = """
    confirmation_id, tenant_id, project_id, principal_id, tool_id,
    params_hash, ceiling_dimension, ceiling_amount, ceiling_note,
    issued_at, expires_at, consumed_at
"""


def _record_from_row(row: tuple) -> ConfirmationRecord:
    return ConfirmationRecord(
        confirmation_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        principal_id=row[3],
        tool_id=row[4],
        params_hash=row[5],
        budget_ceiling=BudgetCeiling(dimension=row[6], amount=row[7], note=row[8]),
        issued_at=row[9],
        expires_at=row[10],
        consumed_at=row[11],
    )


class PostgresConfirmationStore:
    """确认记录的 PostgreSQL 实现。

    接口与内存版 `ConfirmationStore` 对齐（`get` 需要额外的租户上下文，
    因为 RLS 靠它过滤行）。
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ 创建

    def create(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        tool_id: str,
        params: dict,
        budget_ceiling: BudgetCeiling,
        issued_at: datetime,
        ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> ConfirmationRecord:
        if ttl <= timedelta(0):
            raise deny(ErrorCode.POLICY_DENIED, "确认有效期必须为正")

        confirmation_id = new_id("cfm")
        expires_at = issued_at + ttl
        with tenant_transaction(
            tenant_id=tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            conn.execute(
                """
                INSERT INTO confirmations (
                    confirmation_id, tenant_id, project_id, principal_id, tool_id,
                    params_hash, ceiling_dimension, ceiling_amount, ceiling_note,
                    issued_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    confirmation_id,
                    tenant_id,
                    project_id,
                    principal_id,
                    tool_id,
                    content_hash(params),
                    budget_ceiling.dimension,
                    budget_ceiling.amount,
                    budget_ceiling.note,
                    issued_at,
                    expires_at,
                ),
            )
        return ConfirmationRecord(
            confirmation_id=confirmation_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            tool_id=tool_id,
            params_hash=content_hash(params),
            budget_ceiling=budget_ceiling,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------ 读取

    def get(
        self, confirmation_id: str, *, tenant_id: str, project_id: str
    ) -> ConfirmationRecord:
        """读取一条确认。

        RLS 保证只会看到本租户**本项目**的记录：
        传错项目时表现为「不存在」，而不是跨项目读到。
        """
        with tenant_transaction(
            tenant_id=tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM confirmations WHERE confirmation_id = %s",
                (confirmation_id,),
            ).fetchone()
        if row is None:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "确认记录不存在或已失效",
                confirmation_id=confirmation_id,
            )
        return _record_from_row(row)

    def peek(
        self,
        confirmation_id: str,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        tool_id: str,
        params: dict,
        now: datetime,
    ) -> ConfirmationRecord:
        """只校验、不消费。与内存版语义一致（见 `ConfirmationStore.peek`）。"""
        record = self.get(confirmation_id, tenant_id=tenant_id, project_id=project_id)
        validate_binding(
            record,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            tool_id=tool_id,
            params=params,
            now=now,
        )
        return record

    # ------------------------------------------------------------------ 消费

    def consume(
        self,
        confirmation_id: str,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        tool_id: str,
        params: dict,
        now: datetime,
        reserved_budget: dict[str, int] | None = None,
    ) -> ConfirmationRecord:
        """校验并**原子**消费一条确认记录。

        原子性由 `UPDATE ... WHERE consumed_at IS NULL` 保证 ——
        不是靠先查后写的时序（那正是被复现过的缺陷）。
        """
        with tenant_transaction(
            tenant_id=tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM confirmations WHERE confirmation_id = %s",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise deny(
                    ErrorCode.POLICY_DENIED,
                    "确认记录不存在或已失效",
                    confirmation_id=confirmation_id,
                )
            record = _record_from_row(row)

            # 全部绑定项 + 预算上界。预算校验在**占用之前**：
            # 超界被拒时确认不能被消耗掉，否则用户要为一个没执行的动作重新确认。
            validate_binding(
                record,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                tool_id=tool_id,
                params=params,
                now=now,
            )
            if reserved_budget is not None:
                assert_within_ceiling(record.budget_ceiling, reserved_budget)

            claimed = conn.execute(
                f"""
                UPDATE confirmations
                   SET consumed_at = %s
                 WHERE confirmation_id = %s
                   AND consumed_at IS NULL
                RETURNING {_SELECT_COLUMNS}
                """,
                (now, confirmation_id),
            ).fetchone()
            if claimed is None:
                # 并发下另一个事务先占用了它。动作不执行 —— fail-closed。
                raise deny(
                    ErrorCode.POLICY_DENIED,
                    "该确认记录已被使用；确认是一次性的，不能重复执行",
                    confirmation_id=confirmation_id,
                )
            return _record_from_row(claimed)
