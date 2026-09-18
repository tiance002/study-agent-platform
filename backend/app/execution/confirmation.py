"""服务端确认记录。

设计依据：03 号规格 §7（人工确认）、不变量 #3。

**为什么确认必须是服务端记录**：请求体里的 `confirmed_tools` 只能表示
「客户端声称用户确认过」—— 客户端随时可以填，因此它证明不了任何事。

真实确认的形态是：用户在界面上点「确认」→ 服务端创建一条记录 →
执行时由服务端读取并校验。记录必须**同时绑定**下列各项，少一项就可能被复用或走样：

| 绑定项 | 少了它会怎样 |
|---|---|
| 主体 | 别人可以借用你的确认 |
| 租户 + 项目 | 确认可以跨项目复用 |
| 工具 | 确认「改备注」后拿去执行「删库」 |
| **规范化参数哈希** | 确认时看的是参数 A，执行的却是参数 B |
| 预算上限 | 确认时显示 1 元，实际花掉 1000 元 |
| 有效期 | 三个月前的确认仍然有效 |
| 单次消费 | 一条确认被执行多次 |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id

DEFAULT_CONFIRMATION_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class ConfirmationRecord:
    """一条用户确认。它记录的是**事实**：谁、在什么范围、对什么操作点了确认。"""

    confirmation_id: str
    tenant_id: str
    project_id: str
    principal_id: str
    tool_id: str
    params_hash: str
    budget_ceiling: dict
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    def to_dict(self) -> dict:
        return {
            "confirmation_id": self.confirmation_id,
            "project_id": self.project_id,
            "tool_id": self.tool_id,
            "params_hash": self.params_hash,
            "budget_ceiling": self.budget_ceiling,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "consumed": self.consumed,
        }


@dataclass
class ConfirmationStore:
    """确认记录存储。内存实现；生产应由 PostgreSQL 承载并受 RLS 保护。"""

    _records: dict[str, ConfirmationRecord] = field(default_factory=dict)

    # ------------------------------------------------------------------ 创建

    def create(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        tool_id: str,
        params: dict,
        budget_ceiling: dict,
        issued_at: datetime,
        ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> ConfirmationRecord:
        """创建确认记录。参数在创建时就固化，执行时参数不同即失效。"""
        if ttl <= timedelta(0):
            raise deny(ErrorCode.POLICY_DENIED, "确认有效期必须为正")
        record = ConfirmationRecord(
            confirmation_id=new_id("cfm"),
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            tool_id=tool_id,
            params_hash=content_hash(params),
            budget_ceiling=dict(budget_ceiling),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        self._records[record.confirmation_id] = record
        return record

    # ------------------------------------------------------------------ 读取

    def get(self, confirmation_id: str) -> ConfirmationRecord:
        record = self._records.get(confirmation_id)
        if record is None:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "确认记录不存在或已失效",
                confirmation_id=confirmation_id,
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
    ) -> ConfirmationRecord:
        """校验并消费一条确认记录。

        全部绑定项逐一核对 —— **不做"部分匹配就放行"**。
        校验通过后才标记消费，因此校验失败不会浪费用户的确认。
        """
        record = self.get(confirmation_id)

        if record.consumed:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "该确认记录已被使用；确认是一次性的，不能重复执行",
                confirmation_id=confirmation_id,
            )
        if now >= record.expires_at:
            raise deny(
                ErrorCode.POLICY_DENIED,
                f"确认记录已于 {record.expires_at.isoformat()} 过期",
                confirmation_id=confirmation_id,
            )
        if record.principal_id != principal_id:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "确认记录不属于当前用户；确认不能转让",
                confirmation_id=confirmation_id,
            )
        if record.tenant_id != tenant_id or record.project_id != project_id:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "确认记录不属于当前租户或项目；确认不能跨项目复用",
                confirmation_id=confirmation_id,
            )
        if record.tool_id != tool_id:
            raise deny(
                ErrorCode.POLICY_DENIED,
                f"确认记录针对的是 {record.tool_id}，不是 {tool_id}",
                confirmation_id=confirmation_id,
            )
        current_hash = content_hash(params)
        if record.params_hash != current_hash:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "本次参数与确认时的参数不一致；确认只对当时展示的参数有效",
                confirmation_id=confirmation_id,
            )

        consumed = ConfirmationRecord(
            confirmation_id=record.confirmation_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            principal_id=record.principal_id,
            tool_id=record.tool_id,
            params_hash=record.params_hash,
            budget_ceiling=record.budget_ceiling,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            consumed_at=now,
        )
        self._records[confirmation_id] = consumed
        return consumed
