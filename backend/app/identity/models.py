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
from enum import StrEnum

from app.core.contracts import (
    later_than,
    require_aware,
    require_id,
    require_positive,
    require_text,
)


class AuthMethod(StrEnum):
    """How a persistent browser session was authenticated."""

    INVITATION = "invitation"
    PASSWORD = "password"


@dataclass(frozen=True)
class Invitation:
    """A one-time invitation bound to a tenant and principal."""

    invitation_id: str
    tenant_id: str
    token_hash: str
    issued_by: str
    invitee_principal_id: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by: str | None = None

    def __post_init__(self) -> None:
        require_id(self.invitation_id, "invitation_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.token_hash, "token_hash")
        require_id(self.issued_by, "issued_by")
        require_id(self.invitee_principal_id, "invitee_principal_id")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        later_than(self.expires_at, self.issued_at, "expires_at")
        if self.consumed_at is not None:
            require_aware(self.consumed_at, "consumed_at")

    def is_live(self, now: datetime) -> bool:
        return self.consumed_at is None and now < self.expires_at


@dataclass(frozen=True)
class UserSession:
    """A revocable persistent authenticated session."""

    session_id: str
    tenant_id: str
    principal_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    auth_method: AuthMethod = AuthMethod.INVITATION
    credential_id: str | None = None
    security_generation: int | None = None

    def __post_init__(self) -> None:
        require_id(self.session_id, "session_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.principal_id, "principal_id")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        later_than(self.expires_at, self.issued_at, "expires_at")
        if self.revoked_at is not None:
            require_aware(self.revoked_at, "revoked_at")
        if not isinstance(self.auth_method, AuthMethod):
            raise ValueError("auth_method 必须是 AuthMethod")
        if self.auth_method is AuthMethod.INVITATION:
            if self.credential_id is not None:
                raise ValueError("邀请会话不能关联密码凭据")
            generation = self.security_generation
            if generation is None:
                generation = 1
                object.__setattr__(self, "security_generation", generation)
            require_positive(generation, "security_generation")
        else:
            generation = self.security_generation
            if not self.credential_id or generation is None:
                raise ValueError("密码会话必须关联凭据和安全代际")
            require_positive(generation, "security_generation")

    def is_live(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


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
