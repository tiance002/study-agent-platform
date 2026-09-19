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
| **预算上限（真实数值）** | 确认时显示 1 元，实际花掉 1000 元 |
| 有效期 | 三个月前的确认仍然有效 |
| 单次消费 | 一条确认被执行多次 |

## 两种适配器，一套校验

`validate_binding()` 是**模块级**函数而不是某个实现类的私有方法：
内存适配器（`ConfirmationStore`）与 PostgreSQL 适配器必须跑**完全相同**的校验。
校验逻辑各自维护，迟早漂移成「内存版拦截、数据库版放行」——
那是最危险的一种不一致，因为它只在生产环境出现。

## 并发正确性（2026-09-18 审查修复）

原实现是「读 → 判断 → 写」，中间没有互斥。FastAPI 的同步路由运行在线程池里，
两个请求真的会同时进入 `consume()`：两个线程都读到 `consumed_at is None`，
都通过检查，都写入 —— **一条确认被消费两次**。

- 内存适配器：进程内锁把「校验 + 占用」放进同一临界区。
- PostgreSQL 适配器：`UPDATE ... WHERE consumed_at IS NULL RETURNING` ——
  判断和占用是同一条语句，天然原子，**且跨进程有效**
  （进程内锁在多 worker 部署下形同不存在）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id

DEFAULT_CONFIRMATION_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class BudgetCeiling:
    """用户在确认界面上看到的成本上界，执行时被强制校验。

    为什么必须是**数值**而不是一句说明：确认的本质是「用户同意承担这个代价」。
    如果记录里写的是一句 `"source": "node_declared_limit"` 这样的占位说明，
    服务端就无法在执行时回答「这次实际要花的是不是不超过用户看到的数」——
    确认就成了一张金额留空的支票。

    维度用字符串而非枚举，是为了让本模块不反向依赖预算域（层间方向铁律）。
    """

    dimension: str
    amount: int
    note: str = ""

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, "确认的成本上界不能为负")
        if not self.dimension:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, "确认的成本上界必须指明维度")

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "amount": self.amount,
            "note": self.note,
        }


@dataclass(frozen=True)
class ConfirmationRecord:
    """一条用户确认。它记录的是**事实**：谁、在什么范围、对什么操作点了确认。"""

    confirmation_id: str
    tenant_id: str
    project_id: str
    principal_id: str
    tool_id: str
    params_hash: str
    budget_ceiling: BudgetCeiling
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
            "budget_ceiling": self.budget_ceiling.to_dict(),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "consumed": self.consumed,
        }


def validate_binding(
    record: ConfirmationRecord,
    *,
    tenant_id: str,
    project_id: str,
    principal_id: str,
    tool_id: str,
    params: dict,
    now: datetime,
) -> None:
    """全部绑定项逐一核对 —— **不做「部分匹配就放行」**。只读，不改状态。

    模块级函数：内存适配器与 PostgreSQL 适配器共用，保证语义一致。
    """
    if record.consumed:
        raise deny(
            ErrorCode.POLICY_DENIED,
            "该确认记录已被使用；确认是一次性的，不能重复执行",
            confirmation_id=record.confirmation_id,
        )
    if now >= record.expires_at:
        raise deny(
            ErrorCode.POLICY_DENIED,
            f"确认记录已于 {record.expires_at.isoformat()} 过期",
            confirmation_id=record.confirmation_id,
        )
    if record.principal_id != principal_id:
        raise deny(
            ErrorCode.POLICY_DENIED,
            "确认记录不属于当前用户；确认不能转让",
            confirmation_id=record.confirmation_id,
        )
    if record.tenant_id != tenant_id or record.project_id != project_id:
        raise deny(
            ErrorCode.POLICY_DENIED,
            "确认记录不属于当前租户或项目；确认不能跨项目复用",
            confirmation_id=record.confirmation_id,
        )
    if record.tool_id != tool_id:
        raise deny(
            ErrorCode.POLICY_DENIED,
            f"确认记录针对的是 {record.tool_id}，不是 {tool_id}",
            confirmation_id=record.confirmation_id,
        )
    if record.params_hash != content_hash(params):
        raise deny(
            ErrorCode.POLICY_DENIED,
            "本次参数与确认时的参数不一致；确认只对当时展示的参数有效",
            confirmation_id=record.confirmation_id,
        )


def assert_within_ceiling(ceiling: BudgetCeiling, reserved: dict[str, int]) -> None:
    """实际预留不得超过用户确认的上界。"""
    actual = reserved.get(ceiling.dimension, 0)
    if actual > ceiling.amount:
        raise deny(
            ErrorCode.BUDGET_EXCEEDED,
            f"本次执行需要 {actual} {ceiling.dimension}，"
            f"超出用户确认的上界 {ceiling.amount}；"
            f"确认不能覆盖未向用户展示的成本",
            dimension=ceiling.dimension,
            confirmed_ceiling=ceiling.amount,
            requested=actual,
        )


class ConfirmationRepository(Protocol):
    """确认记录的**端口**：内存适配器与 PostgreSQL 适配器互换的契约。

    只声明编排层与接入层真正需要的方法。刻意**不含 `get`**：
    两个适配器的 `get` 签名本就不同（内存按 id 取，PostgreSQL 还必须
    带租户 + 项目上下文、由 RLS 兜底），把它塞进端口等于逼其中一方
    伪造一个自己不满足的签名。端口描述的是共同能力，不是实现细节的并集。

    编排层此前直接标注具体类 `ConfirmationStore`，于是 PG 装配只能靠
    `type: ignore` 或宽联合类型通过 —— 两者都会让"换了适配器却没换对"
    在类型层面不可见。
    """

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
    ) -> ConfirmationRecord: ...

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
    ) -> ConfirmationRecord: ...

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
    ) -> ConfirmationRecord: ...


@dataclass
class ConfirmationStore:
    """确认记录存储（内存适配器）。

    生产由 PostgreSQL 承载（`app.db.confirmation_store.PostgresConfirmationStore`），
    并受 RLS 保护。两种适配器共用 `validate_binding` / `assert_within_ceiling`。
    """

    _records: dict[str, ConfirmationRecord] = field(default_factory=dict)
    # 这把锁替代不了数据库的原子条件更新：它只在单进程内有效。
    # 放进默认值里而不是靠调用方自觉加锁，是为了让「忘记加锁」这件事不可能发生。
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

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
            budget_ceiling=budget_ceiling,
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        with self._lock:
            self._records[record.confirmation_id] = record
        return record

    # ------------------------------------------------------------------ 读取

    def get(self, confirmation_id: str) -> ConfirmationRecord:
        with self._lock:
            record = self._records.get(confirmation_id)
        if record is None:
            raise deny(
                ErrorCode.POLICY_DENIED,
                "确认记录不存在或已失效",
                confirmation_id=confirmation_id,
            )
        return record

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
        """只校验、**不消费**。

        存在的意义是解开一个顺序上的死结：

        - 策略判定需要知道「是否已确认」（`confirmation_recorded` 是策略输入之一）；
        - 但确认不能在策略判定之前消费 —— 否则策略拒绝时，用户的确认已经作废，
          他什么都没执行却要重新确认一次。

        所以前半程用 `peek()` 拿到「确认有效」这个事实，等策略、预算、意图
        全部就绪之后，再用 `consume()` 原子占用。两步之间的窗口由 `consume()`
        的原子性兜住：并发的第二个请求会在占用时失败，动作不执行（fail-closed）。
        """
        record = self.get(confirmation_id)
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

        校验与占用在同一个临界区内完成 —— 拆成两步就回到了被复现过的双消费缺陷。

        `reserved_budget` 是本次执行实际预留的额度。传进来时会被拿来对照
        用户确认时看到的上界：**超出即拒绝**。少了这一步，用户确认的金额
        与实际扣费之间就没有任何约束关系。
        """
        with self._lock:
            record = self.get(confirmation_id)
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
            consumed = replace(record, consumed_at=now)
            self._records[confirmation_id] = consumed
            return consumed
