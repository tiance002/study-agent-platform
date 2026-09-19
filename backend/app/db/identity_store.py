"""身份仓储的 PostgreSQL 适配器（`identity.ports` 的数据库实现）。

三条纪律，与 `db.confirmation_store` 一致：

1. **所有 SQL 参数绑定**。租户、主体、项目、令牌哈希一律走 `%s` 占位符 ——
   这些值哪怕来自服务端断言，拼接也是重新打开注入的门。
2. **业务代码拿不到裸连接**。每个方法内部开 `tenant_transaction` /
   `principal_transaction` / `tenant_only_transaction`，事务边界即方法边界；
   上下文用 `is_local => true`，连接归还时零残留。
3. **数据库错误翻译成平台错误**。适配器是数据库与应用之间唯一的翻译层：
   `UniqueViolation` → 「项目已存在」，组合外键的 `ForeignKeyViolation` →
   「无权访问该项目」。调用方不应知道错误来自哪张表的哪个约束。

## RLS 与各方法的隔离级别

| 方法 | 事务级别 | 原因 |
|---|---|---|
| `create_project` / `grant_project` | 租户级 | 写入供给：创建项目时还没有 project_id 可设 |
| `list_for` / `get` | 租户 + 主体 | `projects` 读取策略是成员感知的（EXISTS 读 `app.principal_id`） |
| `get_live` / `revoke` | 租户 + 主体 | `user_sessions` 策略叠加了主体维度 |
| `exchange` | **无上下文** | 兑换发生在任何身份存在之前（SECURITY DEFINER 函数自己搞定） |
"""

from __future__ import annotations

from datetime import datetime

from psycopg import errors as pg_errors

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.db.session import (
    connect,
    principal_transaction,
    tenant_only_transaction,
)
from app.identity.models import LearningProject, Principal
from app.identity.ports import SystemContext
from app.product.models import Invitation, UserSession

_PROJECT_COLUMNS = (
    "project_id, tenant_id, name, goal, created_at, updated_at, version"
)
_SESSION_COLUMNS = (
    "session_id, tenant_id, principal_id, issued_at, expires_at, revoked_at"
)


def _project_from_row(row: tuple) -> LearningProject:
    return LearningProject(
        project_id=row[0],
        tenant_id=row[1],
        name=row[2],
        goal=row[3],
        created_at=row[4],
        updated_at=row[5],
        version=row[6],
    )


def _session_from_row(row: tuple) -> UserSession:
    return UserSession(
        session_id=row[0],
        tenant_id=row[1],
        principal_id=row[2],
        issued_at=row[3],
        expires_at=row[4],
        revoked_at=row[5],
    )


class PostgresMembershipRepository:
    """成员关系与项目归属的 PostgreSQL 实现。"""

    def __init__(self, clock: Clock | None = None, dsn: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn

    def create_project(
        self,
        context: SystemContext,
        *,
        project_id: str,
        name: str = "",
        goal: str = "",
    ) -> LearningProject:
        stamp = self._clock.now()
        try:
            with tenant_only_transaction(tenant_id=context.tenant_id, dsn=self._dsn) as conn:
                conn.execute(
                    "INSERT INTO projects (project_id, tenant_id, name, goal,"
                    " created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (project_id, context.tenant_id, name, goal, stamp, stamp),
                )
        except pg_errors.UniqueViolation as exc:
            # 与内存实现同一判定、同一句话 —— 两侧语义由契约测试守着。
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"项目已存在：{project_id}") from exc
        # ⚠️ 这里**不能用 `INSERT ... RETURNING`** 拿回行再构造契约：
        # RLS 下 RETURNING 的新行还要通过 SELECT 的 USING 策略，而
        # `projects` 的 USING 是成员感知的 —— 新项目还没有任何授权行，
        # 创建者自己都"看不见"它，RETURNING 直接报 InsufficientPrivilege。
        # 实测踩过：无 RETURNING 的 INSERT 全部放行，加了就稳定失败。
        # 契约对象由入参构造（所有字段都是本次写入的值，无回读必要）。
        return LearningProject(
            project_id=project_id,
            tenant_id=context.tenant_id,
            name=name,
            goal=goal,
            created_at=stamp,
            updated_at=stamp,
        )

    def grant_project(
        self, context: SystemContext, *, principal_id: str, project_id: str
    ) -> None:
        try:
            with tenant_only_transaction(tenant_id=context.tenant_id, dsn=self._dsn) as conn:
                conn.execute(
                    "INSERT INTO project_grants (tenant_id, principal_id, project_id)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (principal_id, project_id) DO NOTHING",
                    (context.tenant_id, principal_id, project_id),
                )
        except pg_errors.ForeignKeyViolation as exc:
            # 组合外键 (tenant_id, principal_id) / (tenant_id, project_id) 拒绝了
            # 「租户 A 的授权行指向租户 B 的主体/项目」。翻译成与内存实现
            # 相同的平台错误，调用方看不到底层约束名。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            ) from exc

    def list_for(self, actor: Principal) -> tuple[LearningProject, ...]:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _PROJECT_COLUMNS + " FROM projects ORDER BY project_id"
            ).fetchall()
        # RLS 的成员感知策略已经把行过滤成"该主体被授予的项目"：
        # 未授予的项目在这里根本不可见，而不是「可见但拒绝」。
        return tuple(_project_from_row(row) for row in rows)

    def get(self, actor: Principal, project_id: str) -> LearningProject:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _PROJECT_COLUMNS + " FROM projects WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            # 不存在 / 跨租户 / 未授予在 RLS 之后不可区分 —— 这正是要的语义。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            )
        return _project_from_row(row)


class PostgresInvitationRepository:
    """邀请的 PostgreSQL 实现。兑换走 `exchange_invitation()` 引导函数。"""

    def __init__(self, clock: Clock | None = None, dsn: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn
        self._sessions = PostgresSessionRepository(clock=self._clock, dsn=dsn)

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
        try:
            with tenant_only_transaction(tenant_id=context.tenant_id, dsn=self._dsn) as conn:
                conn.execute(
                    "INSERT INTO invitations"
                    " (invitation_id, tenant_id, token_hash, issued_by,"
                    "  invitee_principal_id, issued_at, expires_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        invitation_id,
                        context.tenant_id,
                        token_hash,
                        issued_by,
                        invitee_principal_id,
                        issued_at,
                        expires_at,
                    ),
                )
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.PARAMS_INVALID,
                "同一 token_hash 只能签发一次；重复签发会让旧邀请的消费状态失去意义",
            ) from exc
        return Invitation(
            invitation_id=invitation_id,
            tenant_id=context.tenant_id,
            token_hash=token_hash,
            issued_by=issued_by,
            invitee_principal_id=invitee_principal_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def exchange(
        self, token_hash: str, *, session_id: str, session_expires_at: datetime
    ) -> UserSession | None:
        # 刻意不设任何上下文：兑换发生在「调用方还没有任何身份」的时刻。
        # 函数是 SECURITY DEFINER，自己完成"消费邀请 + 建会话"；
        # 应用角色只有 EXECUTE 权限，没有无上下文读表的能力。
        with connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT session_id, tenant_id, principal_id, expires_at"
                " FROM exchange_invitation(%s, %s, %s)",
                (token_hash, session_id, session_expires_at),
            ).fetchall()
            conn.commit()
        if not rows:
            # 未知 / 已过期 / 已消费统一走到这里 —— 与内存版同一个公开结果。
            return None
        _sid, tenant_id, principal_id, _expires = rows[0]
        principal = Principal(principal_id=principal_id, tenant_id=tenant_id)
        # 回读权威行（含 issued_at / revoked_at），而不是用入参拼一个近似对象。
        session = self._sessions.get_live(principal, session_id)
        assert session is not None, (
            "exchange_invitation 返回了会话标识，但紧接着的回读不可见 —— "
            "这说明函数与会话表的隔离策略之间出现了不一致，必须当场暴露"
        )
        return session


class PostgresSessionRepository:
    """会话的 PostgreSQL 实现。撤销与失效判定每次都回库 —— 这是 cookie 可撤销的根基。"""

    def __init__(self, clock: Clock | None = None, dsn: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn

    def create(self, session: UserSession) -> None:
        with principal_transaction(
            tenant_id=session.tenant_id,
            principal_id=session.principal_id,
            dsn=self._dsn,
        ) as conn:
            conn.execute(
                "INSERT INTO user_sessions"
                " (session_id, tenant_id, principal_id, issued_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    session.session_id,
                    session.tenant_id,
                    session.principal_id,
                    session.issued_at,
                    session.expires_at,
                ),
            )

    def get_live(self, actor: Principal, session_id: str) -> UserSession | None:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _SESSION_COLUMNS + " FROM user_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        session = _session_from_row(row)
        return session if session.is_live(self._clock.now()) else None

    def revoke(self, actor: Principal, session_id: str, *, at: datetime) -> bool:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "UPDATE user_sessions SET revoked_at = %s"
                " WHERE session_id = %s AND revoked_at IS NULL"
                " RETURNING session_id",
                (at, session_id),
            ).fetchone()
        return row is not None
