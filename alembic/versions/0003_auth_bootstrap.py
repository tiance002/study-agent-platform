"""认证引导：预绑定邀请、租户绑定的组合外键、最小授权的兑换函数。

第三份迁移，回应审查实测确认的两个漏洞：

## 1. 「子行与父行属于不同租户」没人拦

各表同时存 `tenant_id` 与 `project_id`，但外键只建在 `project_id` 单列上。
审查实测：`tenant X` 的行指向 `tenant Y` 的项目，**超级用户与应用角色都能插进去** ——
外键只看 project_id 存不存在，RLS 的 `WITH CHECK` 只校验本行的 tenant_id 列，
两者都拦不住错配的组合。RLS 是**读**的边界，替代不了**写**的组合完整性。

修法：给 `principals` / `projects` 加 `(tenant_id, …)` 组合唯一键，
再把所有指向它们的单列外键**替换**成组合外键。此后「租户 A 的子行引用
租户 B 的父行」在物理上无法成立 —— 哪怕两个 ID 单独看都合法。

## 2. 邀请兑换没有引导通道

兑换发生在**还没有任何身份**的时刻：客户端只知道一次性令牌，不知道租户。
而邀请表受租户 RLS 保护 —— 实测 `study_app` 无上下文查它是 **0 行**；
同时邀请没绑定被邀请主体，兑换只能靠客户端自报「我是谁」，
这在结构上重新打开了"客户端声明身份"的门。

修法：
- 邀请在**签发时**绑定 `invitee_principal_id`（NOT NULL + 组合外键）；
- 一个 `SECURITY DEFINER` 函数原子地完成「消费邀请 + 建会话」，
  只接受令牌哈希与服务端生成的会话字段 —— **不收租户、不收主体**。

## 函数的权限面（这是它安全的原因）

- 只收**哈希**；原始令牌永不进库、进日志、进审计；
- 只做两件事：消费一条邀请、插一条会话；**不提供任何通用读**；
- `SET search_path = public` 防劫持；`REVOKE` PUBLIC、只 `GRANT EXECUTE`
  给 `study_app` —— 应用能"调用它"，但拿不到"无上下文读表"的能力。

⚠️ **FORCE RLS 与函数 owner**：表是 `FORCE ROW LEVEL SECURITY`，连 owner 都受约束；
而兑换恰恰必须在没有租户上下文时写库。函数以**表 owner**（迁移角色）执行，
本机该角色是超级用户，能绕过 RLS —— **这是显式的运维前提，不是巧合**：
生产迁移角色必须拥有这些表且带 `BYPASSRLS`（或为超级用户），
否则函数会被 FORCE RLS 拦成 0 行，兑换静默失效。
函数的权限面足够小（只收哈希、只做两件事），这是这个前提可以被接受的原因。

## 与 0001/0002 一致的约定

迁移自包含（不共享会演进的 helper）；角色不存在显式失败；downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 直接引用 `projects` 的表：单列外键 → (tenant_id, project_id) 组合外键。
#: 旧约束名按 PostgreSQL 默认命名（`<表>_<列>_fkey`）。
PROJECT_CHILDREN = [
    "project_grants",
    "confirmations",
    "evidence_events",
    "action_intents",
    "conversations",
    "messages",
    "learning_plans",
    "milestones",
    "learning_tasks",
    "sources",
]

#: 引用 `principals` 的 (租户, 主体) 组合外键。NULL 不会被组合外键约束，
#: 所以 `http_idempotency.project_id`（创建项目的命令被占用时还不存在）
#: 与 `invitations.consumed_by`（消费前为空）都不需要特判。
PRINCIPAL_REFERENCES: dict[str, list[str]] = {
    "user_sessions": ["principal_id"],
    "invitations": ["invitee_principal_id", "issued_by", "consumed_by"],
    "http_idempotency": ["principal_id"],
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


def _unique_parent_keys() -> list[str]:
    """组合外键的**目标**必须先存在：父表要有 (tenant_id, 键) 的唯一约束。"""
    return [
        "ALTER TABLE principals"
        " ADD CONSTRAINT principals_tenant_principal_uq UNIQUE (tenant_id, principal_id)",
        "ALTER TABLE projects"
        " ADD CONSTRAINT projects_tenant_project_uq UNIQUE (tenant_id, project_id)",
    ]


def _bind_invitee_column() -> list[str]:
    """给邀请加 `invitee_principal_id` 并收成 NOT NULL。

    列先可空地加进来，显式回填开发数据，再收成 NOT NULL ——
    一步到位的 `ADD COLUMN NOT NULL` 在有历史行的表上会直接失败，
    而"回填谁"这个决定必须显式做出，不能藏在默认值里。
    这里按「已消费记消费人，其余记签发者」回填；两条都缺的行属于数据损坏，
    会被随后的 NOT NULL 拦下。
    """
    return [
        "ALTER TABLE invitations ADD COLUMN invitee_principal_id text",
        "UPDATE invitations SET invitee_principal_id = consumed_by"
        " WHERE invitee_principal_id IS NULL",
        "UPDATE invitations SET invitee_principal_id = issued_by"
        " WHERE invitee_principal_id IS NULL",
        "ALTER TABLE invitations ALTER COLUMN invitee_principal_id SET NOT NULL",
    ]


def _composite_foreign_keys() -> list[str]:
    statements: list[str] = []
    for table in PROJECT_CHILDREN:
        statements.append(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_project_id_fkey"
        )
        statements.append(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_tenant_project_fk"
            f" FOREIGN KEY (tenant_id, project_id)"
            f" REFERENCES projects (tenant_id, project_id)"
        )
    for table, columns in PRINCIPAL_REFERENCES.items():
        for column in columns:
            statements.append(
                f"ALTER TABLE {table} ADD CONSTRAINT"
                f" {table}_{column}_tenant_fk"
                f" FOREIGN KEY (tenant_id, {column})"
                f" REFERENCES principals (tenant_id, principal_id)"
            )
    # http_idempotency 只受"租户+主体"的 RLS 保护，项目维度没有策略兜底 ——
    # 组合外键是它唯一的防线（测试实测过：错配组合原本能插进去）。
    # 它的 project_id 可空（创建项目的命令被占用时项目还不存在），
    # 而 NULL 在组合外键下不受约束 —— 之前的顾虑只对非空值成立。
    statements.append(
        "ALTER TABLE http_idempotency ADD CONSTRAINT http_idempotency_tenant_project_fk"
        " FOREIGN KEY (tenant_id, project_id)"
        " REFERENCES projects (tenant_id, project_id)"
    )
    return statements


def _exchange_function() -> str:
    return """
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
    --
    -- ⚠️ RETURNS TABLE 的列名会成为 PL/pgSQL 变量，裸写 `tenant_id`
    -- 会被替换成那个（值为 NULL 的）变量而不是表列 ——
    -- 所以 RETURNING 里必须用表限定名。这条是跑测试炸出来的。
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

    INSERT INTO user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _function_grants() -> list[str]:
    name = "public.exchange_invitation(text, text, timestamptz)"
    return [
        f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {name} TO {APP_ROLE}",
    ]


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _unique_parent_keys():
        op.execute(statement)
    for statement in _bind_invitee_column():
        op.execute(statement)
    for statement in _composite_foreign_keys():
        op.execute(statement)

    op.execute(_exchange_function())
    for statement in _function_grants():
        op.execute(statement)


def downgrade() -> None:
    # 每一步都要真的还原：函数、组合外键（还原成 0001/0002 的单列形态）、
    # 组合唯一键、邀请的主体绑定列。只删函数不还原外键，
    # 会留下"看起来还原了、完整性却降级了"的中间态。
    name = "public.exchange_invitation(text, text, timestamptz)"
    op.execute(f"DROP FUNCTION IF EXISTS {name}")

    for table, columns in PRINCIPAL_REFERENCES.items():
        for column in columns:
            op.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_tenant_fk"
            )
    for table in reversed(PROJECT_CHILDREN):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_tenant_project_fk")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_project_id_fkey"
            f" FOREIGN KEY (project_id) REFERENCES projects (project_id)"
        )
    op.execute(
        "ALTER TABLE http_idempotency"
        " DROP CONSTRAINT IF EXISTS http_idempotency_tenant_project_fk"
    )

    op.execute("ALTER TABLE invitations DROP CONSTRAINT IF EXISTS invitations_invitee_principal_id_tenant_fk")
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS invitee_principal_id")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_tenant_project_uq")
    op.execute("ALTER TABLE principals DROP CONSTRAINT IF EXISTS principals_tenant_principal_uq")
