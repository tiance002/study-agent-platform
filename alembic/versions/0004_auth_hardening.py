"""认证加固：会话期限硬上限、兑换函数期限守卫、认证限流。

第四份迁移，回应安全审查实测确认的三个缺口：

## 1. 数据库兑换函数接受任意会话期限（P1）

0003 的 `exchange_invitation(p_token_hash, p_session_id, p_session_expires_at)`
对 `p_session_expires_at` 不做任何范围校验，实测应用角色能兑换出
**有效期 100 年**的会话。会话期限由服务端传入，但"服务端会传对"
不是安全边界 —— 配置错误、调用方 bug 都能把超长会话写进库。

修法（双保险，两层缺一不可）：

- 表约束：把 `user_sessions` 上匿名的 `CHECK (expires_at > issued_at)`
  换成**具名**约束 `user_sessions_ttl_check`，上界 30 天，
  对兑换函数写入与直接 INSERT 同样生效；
- 函数守卫：`exchange_invitation` 在插入前显式判定区间
  `(now(), now() + 30 days]`，越界以 `check_violation` 抛出 ——
  适配器把它翻译成内部一致性错误，而不是让一条晦涩的约束失败冒到 API。

30 天与 `app/identity/limits.py::MAX_SESSION_TTL` 是同一个值的两种表示，
由测试对数据库实测钉住；改值必须同时改两处。

## 2. 邀请兑换入口没有限流（P2）

兑换是唯一的未认证写端点，可以被在线穷举令牌或高频打爆。
新增系统表 `auth_attempt_counters`（固定窗口计数）与
`register_auth_attempt()` SECURITY DEFINER 函数：未认证路径没有租户
上下文可设，沿用 0003 兑换函数同一权限模型 —— 应用角色只有
EXECUTE，对计数表没有任何裸表权限。

该表**没有 tenant_id**：它是认证前的系统设施，不属任何租户。
因此不套用租户 RLS 模板（声明式契约生成器通过 `SYSTEM_TABLES`
把它标成「系统级」），但仍 ENABLE + FORCE RLS 且不授任何表权限，
纵深防御与 0003 的运维前提一致（definer 函数以迁移角色执行，
生产迁移角色需 BYPASSRLS 或超级用户，见 0003 说明）。

## 3. 应用无法自检迁移版本（P1 装配收口的前置）

启动自检需要确认"库的迁移版本 == 代码预期"，但应用角色对
`alembic_version` 连 SELECT 都没有。补一条只读授权。
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 会话期限硬上限（天）。必须与 app/identity/limits.py::MAX_SESSION_TTL 一致。
MAX_SESSION_TTL_DAYS = 30

#: 本迁移新建的**系统级**表（无租户、无 RLS 租户模板、不授表权限）。
#: 契约生成器据此把隔离级别标成「系统级」、权限标成「仅 definer 函数」。
SYSTEM_TABLES = ["auth_attempt_counters"]


def _assert_app_role_exists() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; '
        'create it first with scripts/sql/create_app_role.sql';
    END IF;
END
$$
"""
    )


def _exchange_function(with_ttl_guard: bool) -> str:
    """兑换函数。`with_ttl_guard=False` 用于 downgrade 还原 0003 形态。"""
    guard = ""
    if with_ttl_guard:
        guard = f"""
    -- 会话期限硬上限：客户端不提供期限，能越界的只有服务端自身的错误，
    -- 用 check_violation 让适配器能翻译成内部一致性错误而非普通 500。
    IF p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'session expiry outside allowed window (0, %s days]'
            , {MAX_SESSION_TTL_DAYS}
            USING ERRCODE = 'check_violation';
    END IF;
"""
    return f"""
CREATE OR REPLACE FUNCTION public.exchange_invitation(
    p_token_hash    text,
    p_session_id    text,
    p_session_expires_at timestamptz
)
RETURNS TABLE (session_id text, tenant_id text, principal_id text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_tenant_id    text;
    v_principal_id text;
BEGIN
    -- 原子消费：同一邀请的并发兑换在此被行锁串行化，
    -- 第二个事务看到的 consumed_at 已非空，返回空集。
    UPDATE invitations
       SET consumed_at = now(),
           consumed_by = invitee_principal_id
     WHERE token_hash = p_token_hash
       AND consumed_at IS NULL
       AND invitations.expires_at > now()
    RETURNING invitations.tenant_id, invitations.invitee_principal_id
      INTO v_tenant_id, v_principal_id;

    IF v_principal_id IS NULL THEN
        -- 未知 / 已消费 / 过期统一走到这里：
        -- 空结果是调用方唯一可见的事实，不给探针留缝。
        RETURN;
    END IF;
{guard}
    INSERT INTO user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _rate_limit_function() -> str:
    return """
CREATE OR REPLACE FUNCTION public.register_auth_attempt(
    p_bucket text,
    p_window_seconds integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_window_start timestamptz;
    v_attempts     integer;
BEGIN
    IF p_bucket IS NULL OR length(p_bucket) = 0 OR p_window_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid rate limit arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    -- 固定窗口起点：epoch 整除对齐，与应用层 window_start_epoch() 同一公式。
    v_window_start := to_timestamp(
        floor(extract(epoch from clock_timestamp()) / p_window_seconds)
        * p_window_seconds
    );

    INSERT INTO auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
    VALUES (p_bucket, v_window_start, 1, clock_timestamp())
    ON CONFLICT (bucket) DO UPDATE
       SET -- 窗口已滚动就把计数重置为 1，否则在旧值上 +1。
           window_start = CASE WHEN c.window_start < EXCLUDED.window_start
                               THEN EXCLUDED.window_start
                               ELSE c.window_start END,
           attempts     = CASE WHEN c.window_start < EXCLUDED.window_start
                               THEN 1
                               ELSE c.attempts + 1 END,
           last_at      = EXCLUDED.last_at
    RETURNING c.attempts INTO v_attempts;

    RETURN v_attempts;
END;
$fn$;
"""


def upgrade() -> None:
    _assert_app_role_exists()

    # ---- 1. 会话期限硬上限 -------------------------------------------
    # 0002 建表时的匿名 CHECK 自动命名为 user_sessions_check；
    # 换成具名、带上界的约束。具名才让未来的迁移/审查能稳定引用它。
    op.execute("ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_check")
    op.execute(
        "ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_ttl_check"
        " CHECK (expires_at > issued_at"
        f" AND expires_at <= issued_at + interval '{MAX_SESSION_TTL_DAYS} days')"
    )
    op.execute(_exchange_function(with_ttl_guard=True))
    # CREATE OR REPLACE 不改权限，0003 的 GRANT 仍然有效，这里无需重授。

    # ---- 2. 认证限流 ---------------------------------------------------
    op.execute(
        """
CREATE TABLE auth_attempt_counters (
    bucket       text PRIMARY KEY,
    window_start timestamptz NOT NULL,
    attempts     integer NOT NULL CHECK (attempts > 0),
    last_at      timestamptz NOT NULL
)
"""
    )
    # 无租户的系统表：ENABLE + FORCE RLS 且不建任何策略 = 对所有非绕过角色
    # 拒绝一切裸表访问；应用角色只被授予下面 definer 函数的 EXECUTE。
    op.execute("ALTER TABLE auth_attempt_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_attempt_counters FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON auth_attempt_counters FROM PUBLIC")
    op.execute(f"REVOKE ALL ON auth_attempt_counters FROM {APP_ROLE}")
    op.execute(_rate_limit_function())
    op.execute("REVOKE ALL ON FUNCTION public.register_auth_attempt(text, integer) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.register_auth_attempt(text, integer)"
        f" TO {APP_ROLE}"
    )

    # ---- 3. 启动自检需要读迁移版本 ------------------------------------
    op.execute(f"GRANT SELECT ON alembic_version TO {APP_ROLE}")


def downgrade() -> None:
    # 每一步真还原：版本表授权、限流设施、函数守卫、表约束。
    op.execute(f"REVOKE SELECT ON alembic_version FROM {APP_ROLE}")

    op.execute("DROP FUNCTION IF EXISTS public.register_auth_attempt(text, integer)")
    op.execute("DROP TABLE IF EXISTS auth_attempt_counters")

    # 还原成 0003 的函数（无期限守卫）。
    op.execute(_exchange_function(with_ttl_guard=False))

    # 还原成 0002 的匿名 CHECK（PostgreSQL 自动命名为 user_sessions_check）。
    op.execute("ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_ttl_check")
    op.execute("ALTER TABLE user_sessions ADD CHECK (expires_at > issued_at)")
