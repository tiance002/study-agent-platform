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
| `get_live` / `revoke` / `revoke_all_for` | 租户 + 主体 | `user_sessions` 策略叠加了主体维度 |
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

    def create_project_for(
        self, actor: Principal, *, project_id: str, name: str, goal: str
    ) -> LearningProject:
        """创建即授予：两条 INSERT 同一事务。

        用 `principal_transaction`（租户 + 主体）而非 `tenant_only_transaction`：
        授权行的主体维度来自 actor，同一上下文天然覆盖两条写入。
        """
        stamp = self._clock.now()
        try:
            with principal_transaction(
                tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
            ) as conn:
                conn.execute(
                    "INSERT INTO projects (project_id, tenant_id, name, goal,"
                    " created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (project_id, actor.tenant_id, name, goal, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO project_grants (tenant_id, principal_id, project_id)"
                    " VALUES (%s, %s, %s)",
                    (actor.tenant_id, actor.principal_id, project_id),
                )
        except pg_errors.UniqueViolation as exc:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"项目已存在：{project_id}") from exc
        # 契约对象由入参构造（同一事务内已可见，但 RETURNING 在成员感知
        # USING 下不可用 —— 见 create_project 的注释）。
        return LearningProject(
            project_id=project_id,
            tenant_id=actor.tenant_id,
            name=name,
            goal=goal,
            created_at=stamp,
            updated_at=stamp,
        )

    def update(
        self,
        actor: Principal,
        project_id: str,
        *,
        name: str | None,
        goal: str | None,
        expected_version: int,
    ) -> LearningProject:
        stamp = self._clock.now()
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "UPDATE projects"
                " SET name = COALESCE(%s, name), goal = COALESCE(%s, goal),"
                " version = version + 1, updated_at = %s"
                " WHERE project_id = %s AND version = %s"
                " RETURNING " + _PROJECT_COLUMNS,
                (name, goal, stamp, project_id, expected_version),
            ).fetchone()
            if row is None:
                # 零行有两种原因，必须区分：不可见（404 语义）与版本过期（409）。
                # 再查一次可见性 —— RLS 的 USING 会替我们做出区分。
                visible = conn.execute(
                    "SELECT project_id FROM projects WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                if visible is None:
                    raise deny(
                        ErrorCode.CROSS_TENANT_DENIED,
                        "无权访问该项目",
                        project_id=project_id,
                    )
                raise deny(
                    ErrorCode.VERSION_CONFLICT,
                    "项目已被他人修改；请刷新后基于最新版本编辑",
                    expected_version=expected_version,
                )
        return _project_from_row(row)

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

    def __init__(
        self,
        clock: Clock | None = None,
        dsn: str | None = None,
        sessions: PostgresSessionRepository | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn
        # 允许装配层注入**同一个**会话仓储：兑换写入与认证回读必须落在同一
        # 适配器/同一 DSN 上，各建各的实例在传入不同 dsn 时会静默分叉。
        self._sessions = sessions or PostgresSessionRepository(clock=self._clock, dsn=dsn)

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
        try:
            with connect(self._dsn) as conn:
                rows = conn.execute(
                    "SELECT session_id, tenant_id, principal_id, expires_at"
                    " FROM exchange_invitation(%s, %s, %s)",
                    (token_hash, session_id, session_expires_at),
                ).fetchall()
                conn.commit()
        except pg_errors.CheckViolation as exc:
            # 0004 起函数对会话期限做硬上限校验、表上也有同名 CHECK。
            # 能触发它只可能是服务端配置/调用错误（客户端不提供期限），
            # 翻译成内部一致性错误，与内存适配器同一拒绝方向。
            raise deny(
                ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                "会话有效期超出服务端允许范围",
            ) from exc
        if not rows:
            # 未知 / 已过期 / 已消费统一走到这里 —— 与内存版同一个公开结果。
            return None
        _sid, tenant_id, principal_id, _expires = rows[0]
        principal = Principal(principal_id=principal_id, tenant_id=tenant_id)
        # 回读权威行（含 issued_at / revoked_at），而不是用入参拼一个近似对象。
        session = self._sessions.get_live(principal, session_id)
        if session is None:
            # 不用 assert：python -O 会剥掉断言，而这是兑换事务与会话表
            # 隔离策略之间的内部一致性破裂，在优化模式下也必须当场暴露。
            raise deny(
                ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                "兑换函数返回了会话但回读不可见，认证存储一致性破裂",
                session_id=session_id,
            )
        return session


class PostgresSessionRepository:
    """会话的 PostgreSQL 实现。撤销与失效判定每次都回库 —— 这是 cookie 可撤销的根基。"""

    def __init__(self, clock: Clock | None = None, dsn: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn

    def create(self, session: UserSession) -> None:
        try:
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
        except pg_errors.CheckViolation as exc:
            # 0004 的 TTL CHECK：会话期限非法是服务端错误，不是客户端可修复的参数。
            raise deny(
                ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                "会话有效期超出服务端允许范围",
            ) from exc

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

    def revoke_all_for(
        self,
        actor: Principal,
        *,
        at: datetime,
        except_session_id: str | None = None,
    ) -> int:
        """集中失效该主体名下全部存活会话。

        租户+主体维度由 RLS 策略兜底（`app.principal_id` 不匹配的行
        UPDATE 时对当前角色不可见），SQL 里不再手写归属条件，
        与 `revoke` 同一纪律；只额外过滤"存活"与可选例外。
        """
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "UPDATE user_sessions SET revoked_at = %s"
                " WHERE revoked_at IS NULL AND expires_at > now()"
                # `%s::text` 的显式转换不可省：裸参数出现在 `IS NULL` 里时
                # PostgreSQL 无法推断类型，报 "could not determine data type
                # of parameter $2"，整个 logout/all 路径在 PG 形态下 500。
                " AND (%s::text IS NULL OR session_id <> %s)"
                " RETURNING session_id",
                (at, except_session_id, except_session_id),
            ).fetchall()
        return len(rows)
