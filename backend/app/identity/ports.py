"""身份仓储的**端口**（ports）。

与 `product/ports.py` 同一哲学：`Protocol` 结构化类型，内存实现与
PostgreSQL 实现跑同一套契约测试，靠「结构相同」互换，不靠继承。

## 方法签名里"身份从哪来"

这是本模块最重要的设计约束，比方法列表本身重要：

- **访问判定方法**（`list_for` / `get` / `get_live` / `revoke`）的第一个参数
  是 `Principal` —— 服务端断言的身份。端口层不接受裸的 `tenant_id` 字符串，
  否则「谁在问」就退化成「调用方记得传对」。
- **供给方法**（`create_project` / `grant_project` / `issue`）的第一个参数
  是 `SystemContext` —— 显式类型的可信系统上下文（种子脚本、运维动作、
  将来的管理接口）。它和"随手传个 id"在类型上是两回事：
  前者必须显式构造并写明理由，后者在代码审查里一眼就能看出来。

`InvitationRepository.exchange` 是刻意的例外：它**不接收任何身份参数**。
兑换发生在"调用方还没有任何身份"的时刻（这正是邀请存在的意义），
被邀请者由邀请行**预绑定**（0003 迁移的 `invitee_principal_id`），
客户端没有机会通过兑换决定自己是谁。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.contracts import require_id, require_text
from app.identity.models import LearningProject, Principal
from app.product.models import Invitation, UserSession


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

    def get(self, actor: Principal, project_id: str) -> LearningProject:
        """访问判定。**一切失败模式（不存在/跨租户/未授予）返回同一种拒绝**，
        不让人通过错误差异探测别的租户有哪些项目。
        """
        ...


class InvitationRepository(Protocol):
    """一次性邀请的签发与兑换。"""

    def issue(
        self,
        context: SystemContext,
        *,
        invitation_id: str,
        token_hash: str,
        issued_by: str,
        invitee_principal_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> Invitation:
        """签发邀请。`token_hash` 是 `sha256(原始令牌)` —— 原始令牌不落库。"""
        ...

    def exchange(
        self, token_hash: str, *, session_id: str, session_expires_at: datetime
    ) -> UserSession | None:
        """兑换邀请并创建会话 —— **原子且无身份参数**。

        未知 / 已过期 / 已消费返回 `None`（同一种公开结果，不给探针留缝）。
        返回的 `UserSession` 属于邀请**预绑定**的主体。
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
