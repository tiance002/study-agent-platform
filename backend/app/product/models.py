"""产品契约：项目的**从属**实体。

⚠️ 这里**没有项目模型**。项目是 `identity.models.LearningProject` ——
它是归属关系的载体，而"谁能访问哪个项目"由身份层的 `project_grants` 决定。
本模块只放挂在项目下面的东西：会话、消息、计划、里程碑、任务、资料。

两个包各定义一个项目模型的后果是：总有一个会先被改坏而另一个不会。
`tests/test_product_models.py` 里有一条测试专门守着"这里不许出现项目模型"。

## 两处刻意的"没有"

- **`SourceRecord` 没有处理状态**。Round 1 只登记元数据；
  摄取任务与 `processing` / `ready` 这类状态属于 Round 2 的文档处理。
  一个当前永不触发的状态比缺失的状态更糟 —— 它看起来已经实现。
- **`Conversation` 没有 `is_current` 之类的布尔**。需要"当前"时按最大版本/序号算，
  不用一个需要成对更新的标志位（那是另一处 check-then-act）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.contracts import (
    later_than,
    require_aware,
    require_id,
    require_non_negative,
    require_positive,
    require_text,
)


class MessageRole(StrEnum):
    """消息角色。闭集 —— 未知角色必须被拒绝，而不是当成 `user` 放过去。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"


def _require_enum(value: object, expected: type[StrEnum], name: str) -> None:
    """枚举字段必须真的是那个枚举。

    只做 `isinstance` 检查，**不做字符串到枚举的隐式转换** ——
    自动转换会把"调用方传了裸字符串"这个错误藏起来，而它通常意味着
    上游少了一层校验。
    """
    if not isinstance(value, expected):
        raise ValueError(f"{name} 必须是 {expected.__name__}，收到 {value!r}")


@dataclass(frozen=True)
class Conversation:
    """连续问答的容器。

    `last_message_seq` 是**消息序号的原子分配器**：追加消息时用
    `UPDATE ... SET last_message_seq = last_message_seq + 1 ... RETURNING`
    取号，并发追加因此不需要重试循环。
    """

    conversation_id: str
    tenant_id: str
    project_id: str
    title: str
    last_message_seq: int
    created_at: datetime

    def __post_init__(self) -> None:
        require_id(self.conversation_id, "conversation_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_text(self.title, "title", allow_empty=True)
        require_non_negative(self.last_message_seq, "last_message_seq")
        require_aware(self.created_at, "created_at")

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "last_message_seq": self.last_message_seq,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Message:
    """一条消息。**append-only** —— 库层只给应用角色 `SELECT, INSERT`。

    `seq` 由 `Conversation.last_message_seq` 分配，同一会话内唯一且连续。
    用序号而不是时间戳排序：时间戳会重复，而且系统时钟会回拨。
    """

    message_id: str
    tenant_id: str
    project_id: str
    conversation_id: str
    seq: int
    role: MessageRole
    content: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_id(self.message_id, "message_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.conversation_id, "conversation_id")
        require_positive(self.seq, "seq")
        _require_enum(self.role, MessageRole, "role")
        require_text(self.content, "content", allow_empty=True)
        require_aware(self.created_at, "created_at")

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "seq": self.seq,
            "role": str(self.role),
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class LearningPlan:
    """一版学习计划。

    一个项目可以有多版；"当前计划" = `version` 最大的那一行。
    **不用 `is_current` 布尔列** —— 那需要"改这个必须同时改那个"的成对更新。
    """

    plan_id: str
    tenant_id: str
    project_id: str
    version: int
    goal: str
    status: PlanStatus
    created_at: datetime

    def __post_init__(self) -> None:
        require_id(self.plan_id, "plan_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_positive(self.version, "version")
        require_text(self.goal, "goal", allow_empty=True)
        _require_enum(self.status, PlanStatus, "status")
        require_aware(self.created_at, "created_at")

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "goal": self.goal,
            "status": str(self.status),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Milestone:
    """计划里的一个阶段。顺序用**显式 `order_index`**，不用插入顺序。"""

    milestone_id: str
    tenant_id: str
    project_id: str
    plan_id: str
    order_index: int
    title: str
    description: str

    def __post_init__(self) -> None:
        require_id(self.milestone_id, "milestone_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.plan_id, "plan_id")
        require_non_negative(self.order_index, "order_index")
        require_text(self.title, "title")
        require_text(self.description, "description", allow_empty=True)

    def to_dict(self) -> dict:
        return {
            "milestone_id": self.milestone_id,
            "order_index": self.order_index,
            "title": self.title,
            "description": self.description,
        }


@dataclass(frozen=True)
class LearningTask:
    """里程碑下的一个任务。"""

    task_id: str
    tenant_id: str
    project_id: str
    milestone_id: str
    order_index: int
    title: str
    status: TaskStatus

    def __post_init__(self) -> None:
        require_id(self.task_id, "task_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.milestone_id, "milestone_id")
        require_non_negative(self.order_index, "order_index")
        require_text(self.title, "title")
        _require_enum(self.status, TaskStatus, "status")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "order_index": self.order_index,
            "title": self.title,
            "status": str(self.status),
        }


@dataclass(frozen=True)
class PlanBundle:
    """一版计划**及其结构**。整体替换的输入输出单元。

    为什么打包而不是三个独立方法：`PUT /plan` 的语义是"换掉整版计划"。
    分成"写计划 + 写里程碑 + 写任务"三次调用时，中间失败会留下
    半版计划（有计划没里程碑），而它**看起来是合法的**。
    打包成一次替换，让"要么整版换掉、要么什么都不变"成为可实现的目标。
    """

    plan: LearningPlan
    milestones: tuple[Milestone, ...] = ()
    tasks: tuple[LearningTask, ...] = ()

    def __post_init__(self) -> None:
        for milestone in self.milestones:
            if milestone.plan_id != self.plan.plan_id:
                raise ValueError(
                    f"里程碑 {milestone.milestone_id} 属于计划 {milestone.plan_id}，"
                    f"与 bundle 的计划 {self.plan.plan_id} 不一致"
                )
        by_id = {m.milestone_id for m in self.milestones}
        for task in self.tasks:
            if task.milestone_id not in by_id:
                raise ValueError(
                    f"任务 {task.task_id} 指向不在本 bundle 里的里程碑 {task.milestone_id}"
                )


@dataclass(frozen=True)
class SourceRecord:
    """一份资料的**登记**元数据。**没有任何处理状态。**

    `identity_hash` 是服务端从获取方式算出的稳定标识（例如 `sha256(规范化 uri)`），
    同一项目内唯一 —— 去重要靠它，而不是靠显示名（显示名会重复、会改）。
    """

    source_id: str
    tenant_id: str
    project_id: str
    display_name: str
    media_type: str
    identity_hash: str
    registered_at: datetime
    acquisition: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_id(self.source_id, "source_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_text(self.display_name, "display_name")
        require_text(self.media_type, "media_type", allow_empty=True)
        require_id(self.identity_hash, "identity_hash")
        require_aware(self.registered_at, "registered_at")
        if not isinstance(self.acquisition, dict):
            raise TypeError(f"acquisition 必须是字典，收到 {type(self.acquisition).__name__}")

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "registered_at": self.registered_at.isoformat(),
        }


@dataclass(frozen=True)
class Invitation:
    """一次性邀请。**只存哈希** —— 原始令牌不进库、不进日志、不进审计载荷。"""

    invitation_id: str
    tenant_id: str
    token_hash: str
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by: str | None = None

    def __post_init__(self) -> None:
        require_id(self.invitation_id, "invitation_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.token_hash, "token_hash")
        require_id(self.issued_by, "issued_by")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        later_than(self.expires_at, self.issued_at, "expires_at")
        if self.consumed_at is not None:
            require_aware(self.consumed_at, "consumed_at")

    def is_live(self, now: datetime) -> bool:
        return self.consumed_at is None and now < self.expires_at


@dataclass(frozen=True)
class UserSession:
    """数据库支撑的会话。

    撤销是**时间戳**而不是布尔：需要知道"什么时候撤的"，
    而且"是否有效"要能按任意时刻判断（重放排查时用得上）。
    """

    session_id: str
    tenant_id: str
    principal_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        require_id(self.session_id, "session_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.principal_id, "principal_id")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        later_than(self.expires_at, self.issued_at, "expires_at")
        if self.revoked_at is not None:
            require_aware(self.revoked_at, "revoked_at")

    def is_live(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at
