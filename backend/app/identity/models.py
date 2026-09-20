"""身份与项目模型。

`Principal` 是**服务端解析出来的**身份，不是客户端声称的身份。
它一旦被构造，就代表「已通过认证」这一事实，因此不允许从请求字段拼装。

`LearningProject` 是**唯一的项目模型**。它住在身份层而不是产品层，因为
项目首先是**归属关系的载体** ——「谁能访问哪个项目」由 `project_grants` 决定，
而那张表属于身份层。产品层只放项目的从属实体（会话、消息、计划、资料）。

两个包各定义一个 `Project` / `ProjectRecord` 的后果是：总有一个会先被改坏，
而另一个不会。所以这里刻意只留一个，并由
`tests/test_product_models.py` 里的一组测试守着。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.contracts import require_aware, require_id, require_positive, require_text


@dataclass(frozen=True)
class Principal:
    """已认证的主体。身份来自会话令牌，不来自调用方输入。"""

    principal_id: str
    tenant_id: str
    display_name: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)

    def is_admin(self) -> bool:
        return "tenant_admin" in self.roles

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class LearningProject:
    """一个学习项目。**唯一的项目模型。**

    字段顺序把无默认值的放前面（dataclass 的硬要求），
    所以 `goal` / `version` 排在时间戳之后。
    """

    project_id: str
    tenant_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    goal: str = ""
    #: 乐观锁版本号。`PATCH` 必须带上期望版本，旧版本一律拒绝 ——
    #: 靠"最后写入者赢"会让两个人的编辑互相静默覆盖。
    version: int = 1

    def __post_init__(self) -> None:
        require_id(self.project_id, "project_id")
        require_id(self.tenant_id, "tenant_id")
        require_text(self.name, "name", allow_empty=True)
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        require_positive(self.version, "version")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")

    def to_dict(self) -> dict:
        """产品视图。**不含 `tenant_id`** —— 客户端不需要它，暴露它也没有好处。"""
        return {
            "project_id": self.project_id,
            "name": self.name,
            "goal": self.goal,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
