"""产品底座：邀请、会话、会话记录、计划与资料登记。

第二份迁移。它把 Round 1 的产品状态落到 PostgreSQL，
并把 `projects` 的隔离从**租户级**收紧到**成员感知**。

## 三件必须一起做的事

1. 建 9 张产品表：`invitations`、`user_sessions`、`conversations`、`messages`、
   `learning_plans`、`milestones`、`learning_tasks`、`sources`、`http_idempotency`；
2. 给 `projects` 补 `goal` / `version` / `updated_at`；
3. **改写 `projects` 的策略** —— 0001 只按 `tenant_id` 过滤，于是同租户的
   用户 A 能看到用户 B 的项目。成员感知策略补上这一层。

第 3 条是本迁移最关键的部分：它把一条**本来只写在应用层**的规则
（"只能看自己被授权的项目"）搬进数据库。应用层忘了判断，数据库不会忘。

## 为什么 `USING` 与 `WITH CHECK` 不对称

```sql
USING     (tenant_id = app.tenant_id AND EXISTS (存在我的 grant))
WITH CHECK (tenant_id = app.tenant_id)
```

新建项目的那一刻还没有 `project_grants` 行。如果 `WITH CHECK` 也要求成员存在，
**项目永远创建不出来** —— 这是个很典型的"把读权限规则误用到写路径"的错误。

读靠成员关系（安全边界），写靠租户上下文 + 事务内同时写 grant（正确性）。
两条规则管的是两件事，不该长成同一个样子。

## 为什么本文件不 import 0001 的 helper

看起来 `_rls_statements` / `_assert_app_role_exists` 与 0001 重复，是刻意的：
**迁移必须自包含**。若共享一个会演进的 helper，将来改 helper 会让 0001 在
新库上重放出**不同的 SQL** —— 历史迁移的语义就漂移了。
几行重复换"可重放性"，值。

## 与 0001 相同的约定

- 迁移不创建 `study_app`（密码不进仓库），角色不存在时**显式失败**；
- append-only 由 **GRANT** 保证，不靠代码自觉；
- 缺上下文时策略退化成"零行"而不是"全部"。
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

# 本迁移新建、且需要 FORCE RLS 的表，按外键依赖顺序。
TABLES: list[str] = [
    "invitations",
    "user_sessions",
    "conversations",
    "messages",
    "learning_plans",
    "milestones",
    "learning_tasks",
    "sources",
    "http_idempotency",
]

# 项目级：读写上下文必须同时含 tenant 与 project。
PROJECT_SCOPED = {
    "conversations",
    "messages",
    "learning_plans",
    "milestones",
    "learning_tasks",
    "sources",
}

# 只给 SELECT / INSERT 的表 —— append-only 由权限系统保证。
APPEND_ONLY = {"messages"}

# 只给 SELECT / INSERT / UPDATE 的表：要写回结果，但**不该能删除历史**。
NO_DELETE = {"http_idempotency"}

#: 本迁移**改写**了既有表的隔离级别，供 SQL 契约生成器读取。
#:
#: 把它写成模块级常量而不是只藏在函数里，是为了让"这次迁移动了哪张表的隔离语义"
#: 成为一条可被机械读取的事实 —— 否则契约文档里 `projects` 会一直显示成租户级，
#: 而数据库里早就是成员感知了。**文档与实现的漂移，往往就是从这种地方开始的。**
POLICY_OVERRIDES: dict[str, str] = {"projects": "成员感知"}


# --------------------------------------------------------------------- 前置检查


def _assert_app_role_exists() -> None:
    """角色不存在就显式报错。

    与 0001 一致：跳过 GRANT 会得到"迁移成功但应用什么权限都没有"，
    那比失败更难排查。
    """
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


# --------------------------------------------------------------------- 建表


def _create_tables() -> list[str]:
    return [
        # -------------------------------------------------------------- 邀请
        # 只存 token 的哈希。原始令牌不进库、不进日志、不进审计载荷。
        """
CREATE TABLE invitations (
    invitation_id text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    token_hash    text NOT NULL UNIQUE,
    issued_by     text NOT NULL,
    issued_at     timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL,
    consumed_at   timestamptz,
    consumed_by   text,
    CHECK (expires_at > issued_at)
)
""",
        # -------------------------------------------------------------- 会话
        # cookie 里只放引用了 session_id 的签名不透明值；身份每次从这一行读，
        # 撤销才能立刻生效。
        """
CREATE TABLE user_sessions (
    session_id   text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
    principal_id text NOT NULL,
    issued_at    timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    CHECK (expires_at > issued_at)
)
""",
        # -------------------------------------------------------------- 会话记录
        # last_message_seq 是**消息序号的原子分配器**：
        # UPDATE ... SET last_message_seq = last_message_seq + 1 RETURNING
        # 并发追加因此不需要重试循环。
        """
CREATE TABLE conversations (
    conversation_id  text PRIMARY KEY,
    tenant_id        text NOT NULL REFERENCES tenants (tenant_id),
    project_id       text NOT NULL REFERENCES projects (project_id),
    title            text NOT NULL DEFAULT '',
    last_message_seq bigint NOT NULL DEFAULT 0 CHECK (last_message_seq >= 0),
    created_at       timestamptz NOT NULL DEFAULT now()
)
""",
        # 消息 append-only：应用角色只有 SELECT / INSERT（见 GRANT 段）。
        # 用序号而不是时间戳排序：时间戳会重复，系统时钟也会回拨。
        """
CREATE TABLE messages (
    message_id      text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL REFERENCES projects (project_id),
    conversation_id text NOT NULL REFERENCES conversations (conversation_id),
    seq             bigint NOT NULL CHECK (seq > 0),
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
)
""",
        # 一个项目可有多版计划；"当前"= version 最大的那一行。
        # 刻意没有 is_current 布尔列 —— 它需要成对更新，是另一处 check-then-act。
        """
CREATE TABLE learning_plans (
    plan_id    text PRIMARY KEY,
    tenant_id  text NOT NULL REFERENCES tenants (tenant_id),
    project_id text NOT NULL REFERENCES projects (project_id),
    version    integer NOT NULL CHECK (version > 0),
    goal       text NOT NULL,
    status     text NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, version)
)
""",
        # 顺序用显式 order_index：序号是内容的一部分，且要支持拖动排序。
        """
CREATE TABLE milestones (
    milestone_id text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
    project_id   text NOT NULL REFERENCES projects (project_id),
    plan_id      text NOT NULL REFERENCES learning_plans (plan_id),
    order_index  integer NOT NULL CHECK (order_index >= 0),
    title        text NOT NULL,
    description  text NOT NULL DEFAULT '',
    UNIQUE (plan_id, order_index)
)
""",
        """
CREATE TABLE learning_tasks (
    task_id      text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
    project_id   text NOT NULL REFERENCES projects (project_id),
    milestone_id text NOT NULL REFERENCES milestones (milestone_id),
    order_index  integer NOT NULL CHECK (order_index >= 0),
    title        text NOT NULL,
    status       text NOT NULL
        CHECK (status IN ('pending', 'in_progress', 'done', 'skipped')),
    UNIQUE (milestone_id, order_index)
)
""",
        # ⚠️ 本表**没有任何处理状态**。「已登记」就是它知道的全部事实。
        # Round 2 引入摄取任务与切块时会新增状态列，那时的迁移里再出现
        # processing / ready —— 现在不预留，因为它们当前永不产生，
        # 而一个永不触发的状态比缺失的状态更糟：它看起来已经实现。
        """
CREATE TABLE sources (
    source_id     text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    project_id    text NOT NULL REFERENCES projects (project_id),
    display_name  text NOT NULL,
    media_type    text NOT NULL DEFAULT '',
    identity_hash text NOT NULL,
    acquisition   jsonb NOT NULL DEFAULT '{}'::jsonb,
    registered_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, identity_hash)
)
""",
        # -------------------------------------------------------------- 命令幂等
        # HTTP 命令幂等的**唯一**判定出口。
        #
        # 唯一键是 (tenant_id, principal_id, command_scope, client_key)：
        #   少主体 → 猜到 key 就能读到别人的响应；
        #   少命令 → 同一把钥匙在两个端点上互相串味；
        #   少租户 → 跨租户可用性干扰。
        #
        # ⚠️ project_id **不进唯一键、也不建外键**：创建项目的那个命令被占用时
        # 项目还不存在。它的用途只是范围过滤与审计。
        """
CREATE TABLE http_idempotency (
    claim_id      text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    principal_id  text NOT NULL,
    command_scope text NOT NULL,
    client_key    text NOT NULL,
    request_hash  text NOT NULL,
    project_id    text,
    state         text NOT NULL CHECK (state IN ('pending', 'completed', 'released')),
    status_code   integer,
    response_body jsonb,
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    UNIQUE (tenant_id, principal_id, command_scope, client_key)
)
""",
    ]


# --------------------------------------------------------------------- 索引


def _index_statements() -> list[str]:
    return [
        # 成员感知策略的支撑索引。缺了它，项目列表会退化成
        # "全表扫 + 逐行 EXISTS"，而策略本身是对的却慢得不能用。
        "CREATE INDEX project_grants_principal_idx ON project_grants (principal_id, project_id)",
        # 会话清理与存活查询。
        "CREATE INDEX user_sessions_expiry_idx ON user_sessions (tenant_id, expires_at)",
        "CREATE INDEX user_sessions_live_idx ON user_sessions (session_id)"
        " WHERE revoked_at IS NULL",
        # 邀请清理：只索引尚未消费的。
        "CREATE INDEX invitations_live_idx ON invitations (expires_at) WHERE consumed_at IS NULL",
        # 消息按会话稳定排序。
        "CREATE INDEX messages_conversation_seq_idx ON messages (conversation_id, seq)",
        # 取某项目的最新计划版本。
        "CREATE INDEX learning_plans_project_version_idx"
        " ON learning_plans (project_id, version DESC)",
        # 资料按项目列出。
        "CREATE INDEX sources_project_idx ON sources (project_id, registered_at)",
        # 幂等表的过期清理用（保留策略不在本轮实现，索引先备好）。
        "CREATE INDEX http_idempotency_claim_age_idx ON http_idempotency (claimed_at)",
    ]


# --------------------------------------------------------------------- RLS


def _standard_rls(table: str, *, project_scoped: bool = False) -> list[str]:
    """一般表的 RLS 语句。

    谓词与 0001 一致：缺上下文时 `app.tenant_id` 为 NULL，
    `tenant_id = NULL` 恒假 —— 忘记设置上下文退化成"查不到"，
    而不是"查到全部"。
    """
    predicate = "tenant_id = current_setting('app.tenant_id', true)"
    if project_scoped:
        predicate = (
            f"({predicate})\n"
            f"       AND project_id = current_setting('app.project_id', true)"
        )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _projects_membership_policy(*, restore_tenant_only: bool) -> list[str]:
    """`projects` 的策略：成员感知版，或还原成 0001 的租户级版。

    两个方向写在同一个函数里，是为了让"改成什么"与"还原成什么"**永远成对出现** ——
    分开写时，最容易发生的事就是升级改了、降级忘了还原，
    留下一张带 FORCE RLS 却没有策略（等于拒绝一切访问）的表。
    """
    if restore_tenant_only:
        predicate = "tenant_id = current_setting('app.tenant_id', true)"
        return [
            "DROP POLICY IF EXISTS projects_isolation ON projects",
            "CREATE POLICY projects_isolation ON projects\n"
            f"    USING ({predicate})\n"
            f"    WITH CHECK ({predicate})",
        ]
    return [
        "DROP POLICY IF EXISTS projects_isolation ON projects",
        "CREATE POLICY projects_isolation ON projects\n"
        "    USING (\n"
        "        tenant_id = current_setting('app.tenant_id', true)\n"
        "        AND EXISTS (\n"
        "            SELECT 1 FROM project_grants g\n"
        "            WHERE g.project_id = projects.project_id\n"
        "              AND g.principal_id = current_setting('app.principal_id', true)\n"
        "        )\n"
        "    )\n"
        "    WITH CHECK (\n"
        "        tenant_id = current_setting('app.tenant_id', true)\n"
        "    )",
    ]


# --------------------------------------------------------------------- 授权


def _grant_statements() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        if table in APPEND_ONLY:
            statements.append(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
        elif table in NO_DELETE:
            statements.append(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}")
        else:
            statements.append(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
            )
    # 本迁移没有 identity 列（主键都是 text），但保持与 0001 一致，
    # 免得将来加自增列时忘了这一句。
    statements.append(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    return statements


# --------------------------------------------------------------------- 迁移


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _create_tables():
        op.execute(statement)

    for table in TABLES:
        for statement in _standard_rls(table, project_scoped=table in PROJECT_SCOPED):
            op.execute(statement)

    # projects 的策略换成成员感知版。
    for statement in _projects_membership_policy(restore_tenant_only=False):
        op.execute(statement)

    # projects 的产品字段。`NOT NULL DEFAULT` 在 PG 11+ 是元数据操作，不重写表。
    op.execute("ALTER TABLE projects ADD COLUMN goal text NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE projects ADD COLUMN version integer NOT NULL DEFAULT 1 "
        "CHECK (version > 0)"
    )
    op.execute(
        "ALTER TABLE projects ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )

    for statement in _index_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    # 顺序与 upgrade 相反，且**每一处都要真的还原**：
    #
    # - projects 的索引先删（列删掉后索引会自动消失，但显式删更清楚）；
    # - 再把策略还原成 0001 的租户级版本 —— 只 DROP POLICY 不重建，
    #   会留下一张带 FORCE RLS 却没有策略的表，那等于拒绝一切访问；
    # - 最后删列与删表。
    op.execute("DROP INDEX IF EXISTS project_grants_principal_idx")

    for statement in _projects_membership_policy(restore_tenant_only=True):
        op.execute(statement)

    for column in ("updated_at", "version", "goal"):
        op.execute(f"ALTER TABLE projects DROP COLUMN IF EXISTS {column}")

    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
