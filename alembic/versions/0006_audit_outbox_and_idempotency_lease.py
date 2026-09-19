"""审查修复：幂等租约、认证审计 outbox、definer 函数 schema 限定。

第六份迁移，回应第 1/2 轮安全审查确认的四个缺口：

## 1. HTTP 幂等的租约与不确定态（第 2 轮 P1）

`http_idempotency` 的 `pending` 行在持有进程崩溃后**永远**无法恢复：
claim 只允许 `released` 重新占用，从不处理超时的 `pending`。
本迁移补 `owner_token` 与 `indeterminate` 状态：超过租约期只证明持有者
失联，不能证明业务没有提交，因此 pending 原子进入 indeterminate，调用方
必须先查询资源状态或由支持流程对账。项目、消息和计划都没有足以恢复原响应的
领域唯一约束，自动接管会重复业务，禁止使用。

## 2. 认证审计的 transactional outbox（第 1 轮 P1）

邀请兑换先提交业务、后写审计：审计 sink 不可用时返回 503，
但邀请已消费、会话已创建 —— fail-closed 名存实亡。
新增 `auth_audit_outbox`：兑换成功路径的审计事件由仓储在**业务同一事务**
写入本表，提交即事实落库；投影进链式 sink 是提交后的尽力而为，
失败留下 `projected_at IS NULL` 的可观测积压，绝不丢事实。

应用角色需要 INSERT（兑换事务内写）、SELECT / UPDATE（投影），
但**没有 DELETE** —— outbox 也是 append-only 事实的一部分。

## 3. SECURITY DEFINER 函数未限定 schema（第 1 轮 P2）

0004 的两个 definer 函数虽设 `search_path = public`，函数体仍用
未限定的 `invitations` / `user_sessions` / `auth_attempt_counters`。
在保留 `PUBLIC CREATE` 权限的库上存在对象劫持条件。
本迁移以 `SET search_path = pg_catalog` + 全限定表名重建两个函数；
`downgrade()` 如实还原 0004 形态。

## 4. 限流计数桶无界增长（第 1 轮 P1 的存储半边）

`auth_attempt_counters.bucket` 是无界 text 主键且从不清理。
函数内追加**机会式清理**：写入时顺手删除 24 小时前的旧窗口行
（桶键长度本身由应用层修 —— 只允许可解析的 IP 进入桶键）。

## 与 0001-0005 一致的约定

迁移自包含（不共享会演进的 helper）；角色不存在显式失败；downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 会话期限硬上限（天）。必须与 app/identity/limits.py::MAX_SESSION_TTL 一致。
MAX_SESSION_TTL_DAYS = 30

#: outbox 是审计事实的中转：应用可 INSERT / SELECT / UPDATE（投影标记），
#: 契约生成器按 NO_DELETE 处理；scope 标签用 POLICY_OVERRIDES 校正。
NO_DELETE = {"auth_audit_outbox"}
POLICY_OVERRIDES = {
    "auth_audit_outbox": "系统级（认证前审计事实中转；应用 INSERT/SELECT/UPDATE，无 DELETE）",
}


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


def _exchange_function() -> str:
    """兑换函数（0004 形态 + schema 全限定 + search_path 收窄到 pg_catalog）。"""
    return f"""
CREATE OR REPLACE FUNCTION public.exchange_invitation(
    p_token_hash    text,
    p_session_id    text,
    p_session_expires_at timestamptz
)
RETURNS TABLE (session_id text, tenant_id text, principal_id text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_tenant_id    text;
    v_principal_id text;
BEGIN
    -- 会话期限硬上限（0004 引入的守卫，原样保留）。
    IF p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'session expiry outside allowed window (0, %s days]'
            , {MAX_SESSION_TTL_DAYS}
            USING ERRCODE = 'check_violation';
    END IF;

    -- 原子消费：同一邀请的并发兑换在此被行锁串行化。
    -- 表名全限定：definer 函数在受限 search_path 下运行，
    -- 未限定名是对象劫持的经典入口（审查 P2）。
    UPDATE public.invitations
       SET consumed_at = now(),
           consumed_by = invitee_principal_id
     WHERE token_hash = p_token_hash
       AND consumed_at IS NULL
       AND public.invitations.expires_at > now()
    RETURNING public.invitations.tenant_id, public.invitations.invitee_principal_id
      INTO v_tenant_id, v_principal_id;

    IF v_principal_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _rate_limit_function() -> str:
    """限流函数（0004 形态 + schema 全限定 + 机会式旧桶清理）。"""
    return """
CREATE OR REPLACE FUNCTION public.register_auth_attempt(
    p_bucket text,
    p_window_seconds integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
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

    -- 机会式清理：桶键曾无界增长（每次轮换伪造 IP 都能造新桶），
    -- 应用层已把桶键收窄为可解析 IP；这里再按时间兜底清理旧窗口。
    DELETE FROM public.auth_attempt_counters
     WHERE window_start < now() - interval '24 hours';

    INSERT INTO public.auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
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


def _exchange_function_0004_shape() -> str:
    """0004 的函数形态（downgrade 还原用：search_path=public、未限定表名）。"""
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
    IF p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'session expiry outside allowed window (0, %s days]'
            , {MAX_SESSION_TTL_DAYS}
            USING ERRCODE = 'check_violation';
    END IF;

    UPDATE invitations
       SET consumed_at = now(),
           consumed_by = invitee_principal_id
     WHERE token_hash = p_token_hash
       AND consumed_at IS NULL
       AND invitations.expires_at > now()
    RETURNING invitations.tenant_id, invitations.invitee_principal_id
      INTO v_tenant_id, v_principal_id;

    IF v_principal_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _rate_limit_function_0004_shape() -> str:
    """0004 的限流函数形态（downgrade 还原用：无清理、未限定表名）。"""
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

    v_window_start := to_timestamp(
        floor(extract(epoch from clock_timestamp()) / p_window_seconds)
        * p_window_seconds
    );

    INSERT INTO auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
    VALUES (p_bucket, v_window_start, 1, clock_timestamp())
    ON CONFLICT (bucket) DO UPDATE
       SET window_start = CASE WHEN c.window_start < EXCLUDED.window_start
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

    # ---- 1. 幂等租约 ---------------------------------------------------
    # owner_token：占用者身份。进入 indeterminate 后，旧持有者的
    # complete/release 不再改写结果。
    op.execute(
        "ALTER TABLE http_idempotency ADD COLUMN owner_token text"
    )
    op.execute(
        "ALTER TABLE http_idempotency DROP CONSTRAINT IF EXISTS http_idempotency_state_check"
    )
    op.execute(
        "ALTER TABLE http_idempotency ADD CONSTRAINT http_idempotency_state_check"
        " CHECK (state IN ('pending', 'completed', 'released', 'indeterminate'))"
    )

    # ---- 2. 认证审计 outbox -------------------------------------------
    # 无租户模板的系统级表：兑换发生在任何身份存在之前，行由 definer
    # 事务写入；投影由应用角色执行（因此有 SELECT/UPDATE 权限，与
    # auth_attempt_counters 的"零表权限"不同 —— 那张表应用完全碰不到）。
    op.execute(
        """
CREATE TABLE auth_audit_outbox (
    event_id     text PRIMARY KEY,
    event_type   text NOT NULL,
    payload      jsonb NOT NULL,
    risk         text NOT NULL CHECK (risk IN ('low', 'high')),
    tenant_id    text NOT NULL REFERENCES public.tenants (tenant_id),
    project_id   text,
    request_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    projected_at timestamptz
)
"""
    )
    op.execute("ALTER TABLE auth_audit_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_audit_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY auth_audit_outbox_isolation ON auth_audit_outbox"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(f"GRANT INSERT, SELECT, UPDATE ON auth_audit_outbox TO {APP_ROLE}")
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.auth_audit_pending_count()
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
    SELECT count(*) FROM public.auth_audit_outbox WHERE projected_at IS NULL
$fn$
"""
    )
    op.execute("REVOKE ALL ON FUNCTION public.auth_audit_pending_count() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.auth_audit_pending_count() TO {APP_ROLE}")

    # ---- 3. definer 函数：schema 全限定 + search_path 收窄 + 清理 -------
    op.execute(_exchange_function())
    op.execute(_rate_limit_function())
    # CREATE OR REPLACE 不改权限，0004 的 GRANT/REVOKE 仍然有效。


def downgrade() -> None:
    # 每一步真还原：outbox 表、owner_token 列、0004 形态的函数。
    op.execute("DROP FUNCTION IF EXISTS public.auth_audit_pending_count()")
    op.execute("DROP TABLE IF EXISTS auth_audit_outbox")
    op.execute(
        "ALTER TABLE http_idempotency DROP CONSTRAINT IF EXISTS http_idempotency_state_check"
    )
    op.execute(
        "ALTER TABLE http_idempotency ADD CONSTRAINT http_idempotency_state_check"
        " CHECK (state IN ('pending', 'completed', 'released'))"
    )
    op.execute("ALTER TABLE http_idempotency DROP COLUMN IF EXISTS owner_token")

    op.execute(_exchange_function_0004_shape())
    op.execute(_rate_limit_function_0004_shape())
