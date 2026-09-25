"""身份仓储的**端口**（ports）。

与 `product/ports.py` 同一哲学：`Protocol` 结构化类型，内存实现与
PostgreSQL 实现跑同一套契约测试，靠「结构相同」互换，不靠继承。

## 方法签名里"身份从哪来"

这是本模块最重要的设计约束，比方法列表本身重要：

- **访问判定方法**（`list_for` / `get` / `get_live` / `revoke`）的第一个参数
  是 `Principal` —— 服务端断言的身份。端口层不接受裸的 `tenant_id` 字符串，
  否则「谁在问」就退化成「调用方记得传对」。
- **供给方法**（`create_project` / `grant_project`）的第一个参数
  是 `SystemContext` —— 显式类型的可信系统上下文（种子脚本、运维动作、
  将来的管理接口）。它和"随手传个 id"在类型上是两回事：
  前者必须显式构造并写明理由，后者在代码审查里一眼就能看出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.contracts import require_id, require_text
from app.identity.models import LearningProject, Principal, UserSession


@dataclass(frozen=True)
class AccountLookup:
    credential_id: str
    tenant_id: str
    principal_id: str
    username: str
    password_hash: str
    hash_version: int
    security_generation: int
    disabled_at: datetime | None = None


@dataclass(frozen=True)
class AccountRegistrationResult:
    lookup: AccountLookup
    session: UserSession
    default_project_id: str
    default_project_name: str


class AccountRepository(Protocol):
    """Credential lookup and atomic account registration boundary."""

    def lookup(self, username_normalized: str) -> AccountLookup | None: ...

    def register(
        self,
        *,
        username_original: str,
        username_normalized: str,
        password_hash: str,
        hash_version: int,
        session_expires_at: datetime,
    ) -> AccountRegistrationResult: ...

    def complete_login(
        self,
        *,
        lookup: AccountLookup,
        session_expires_at: datetime,
        new_password_hash: str | None = None,
        new_hash_version: int | None = None,
    ) -> UserSession | None: ...


@dataclass(frozen=True)
class SystemContext:
    """显式可信的系统上下文（种子、运维、后台任务）。

    为什么不接受裸字符串：`create_project("t_1", ...)` 和
    `create_project(SystemContext("t_1", "演示种子"), ...)` 在调用点上
    是两种可读性完全不同的代码 —— 后者逼着作者回答"这次写入是谁授权的"。
    """

    tenant_id: str
    reason: str = "system"

    def __post_init__(self) -> None:
        require_id(self.tenant_id, "tenant_id")
        require_text(self.reason, "reason", allow_empty=True)


class MembershipRepository(Protocol):
    """项目归属与访问判定。**项目边界真正的执行点是 `get`。**"""

    def create_project(
        self,
        context: SystemContext,
        *,
        project_id: str,
        name: str = "",
        goal: str = "",
    ) -> LearningProject: ...

    def grant_project(
        self, context: SystemContext, *, principal_id: str, project_id: str
    ) -> None:
        """授予访问权。跨租户授予必须被拒绝（内存是显式判定，PG 是组合外键）。"""
        ...

    def list_for(self, actor: Principal) -> tuple[LearningProject, ...]:
        """该主体被授予的项目，按 project_id 稳定排序。返回完整契约。"""
        ...

    def create_project_for(
        self, actor: Principal, *, project_id: str, name: str, goal: str
    ) -> LearningProject:
        """产品路径的建项目：**创建即授予创建者**，一个不可分的行为。

        与 `create_project`（SystemContext 供给路径）的区别不在权限而在
        原子性：没有"建了项目但自己看不见"的窗口 —— PostgreSQL 实现
        必须把 projects 与 project_grants 两条 INSERT 放进同一事务。
        """
        ...

    def update(
        self,
        actor: Principal,
        project_id: str,
        *,
        name: str | None,
        goal: str | None,
        expected_version: int,
    ) -> LearningProject:
        """乐观锁更新。`expected_version` 不匹配抛 VERSION_CONFLICT；
        项目不可见（RLS 过滤后无行）与一切访问失败同语义 —— 无权访问。
        """
        ...

    def get(self, actor: Principal, project_id: str) -> LearningProject:
        """访问判定。**一切失败模式（不存在/跨租户/未授予）返回同一种拒绝**，
        不让人通过错误差异探测别的租户有哪些项目。
        """
        ...


class SessionRepository(Protocol):
    """数据库支撑的会话。撤销立刻生效的关键：认证路径每次都回库查询。"""

    def create(self, session: UserSession) -> None: ...

    def get_live(self, actor: Principal, session_id: str) -> UserSession | None:
        """按会话 id 查存活会话。

        `actor` 来自**已验签**的 cookie 声明。会话不存在 / 已撤销 / 已过期 /
        属于别的主体 —— 一律返回 `None`，调用方（认证层）统一拒绝。
        """
        ...

    def revoke(self, actor: Principal, session_id: str, *, at: datetime) -> bool:
        """撤销会话。返回是否真的撤掉了一条（重复撤销返回 False）。"""
        ...

    def revoke_all_for(
        self,
        actor: Principal,
        *,
        at: datetime,
        except_session_id: str | None = None,
    ) -> int:
        """集中失效：撤销该主体名下全部存活会话（"退出所有设备"）。

        只作用于调用方自己的租户+主体（RLS / 归属判定兜底），
        返回本次真正撤掉的条数。`except_session_id` 用于"撤掉其余设备
        但保留当前会话"；集中轮换密钥等场景不传例外。
        """
        ...
