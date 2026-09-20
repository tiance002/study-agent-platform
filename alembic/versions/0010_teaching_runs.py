"""模型教学交互：运行、provider attempt、事件与预算。

第十份迁移，为第五轮「持久化模型教学交互」补五张表与两处父键。

## 表的分工（与计划任务 2 冻结的关系一致）

| 表 | 回答的问题 | study_app 权限 | study_worker 权限 |
|---|---|---|---|
| `teaching_runs` | 一次教学运行到哪一步、引用哪条问题/答案消息 | SELECT, INSERT, UPDATE | SELECT, UPDATE |
| `provider_attempts` | 那次 provider 调用的派发与结果事实 | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE |
| `teaching_events` | 给 SSE 回放用的稳定事件流（append-only） | SELECT, INSERT | SELECT, INSERT |
| `teaching_budgets` | 项目级预算账户（整数微单位记账） | SELECT, INSERT, UPDATE | SELECT, UPDATE |
| `teaching_tenant_budgets` | 租户级总额度（跨项目聚合的**物化**事实） | SELECT, INSERT, UPDATE | SELECT, UPDATE |
| `teaching_reservations` | 一笔费用预留的生命周期 | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE |

## 状态机（与计划冻结的图一致）

```text
queued → running → succeeded
queued → failed                   # 可证明未派发
running → failed                  # provider 明确失败且费用可定
running → reconciliation_required # 已派发、结果或费用未知
```

queued 阶段按 fencing 规则接管；`claim_token` 与 0009 的摄取围栏同构。
发生 provider 派发之后（`provider_attempts` 有一行），租约到期**不得**自动
重新派发 —— `provider_attempts` 的 `UNIQUE (tenant_id, project_id, run_id)`
在物理上禁止了第二次派发，"自动调用次数仍为 1"由数据库兜底，不靠 worker 自觉。

## 关键 CHECK 与 Python 契约是同一条规则的两个出口

与 0007 相同的取舍：`running ↔ 租约三件套`、`failed/reconciliation_required ↔
错误码`、`succeeded ↔ 答案消息 + grounding`。卡死的运行与"成功但没有消息"
都是不可修复的状态，数据库必须能拒收。

## 为什么 run 行冗余存着 `question` 文本

`user_message_id` 是权威引用（组合外键 + 唯一），但 worker 构建上下文时
需要问题文本 —— 不冗余的话 worker 就要 `SELECT` 整张 `messages` 表，
凭据边界为此扩大一张表不值得。`messages` 是 append-only 的不可变行，
副本因此是**稳定的**，不存在漂移问题。

## 为什么答案消息由 worker 插入（而不回应用角色事务）

`finish_run` 必须**同事务**写：唯一 assistant 消息、费用结算、终态事件、
run 终态。拆成两个事务就是"已成功但无消息"的温床。worker 因此获得
`messages` 的 `INSERT`（仅 INSERT —— append-only 边界不破），行级范围
仍由 `messages` 的 RLS（租户 + 项目上下文，取自 run 行）约束。

答案消息的 `seq` 在 `start_run`（应用角色事务）里**预分配**：
`last_message_seq` 原子 +2，一个给用户问题、一个留给答案。运行失败时
这个序号作废（出现空洞）—— `seq` 是排序不是账目，空洞无害；
换来的好处是 worker 落定时**不需要** `conversations` 的 UPDATE 权限。

## 预算是整数微单位，禁止浮点

`*_micro` 全部是 bigint：浮点累计会丢分位（0.1 + 0.2 ≠ 0.3），
钱的事实不允许这样。价格快照带版本（`teaching-price/v1`），
改价 = 新版本 + 新快照，不改历史行。

## 组合外键（铁律 36）

`conversations` / `messages` 补上 `(tenant_id, project_id, <id>)` 组合唯一键
作为父键：「租户 A 的运行挂在租户 B 的会话上」物理上无法成立。
`downgrade` 时显式删除这两个约束，不留 0010 的痕迹。

与 0001-0009 一致：迁移自包含、角色不存在显式失败、downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"
WORKER_ROLE = "study_worker"

TABLES: list[str] = [
    "teaching_runs",
    "provider_attempts",
    "teaching_events",
    "teaching_budgets",
    "teaching_tenant_budgets",
    "teaching_reservations",
]

#: 项目级：读写上下文必须同时含 tenant 与 project。
PROJECT_SCOPED = {
    "teaching_runs",
    "provider_attempts",
    "teaching_events",
    "teaching_budgets",
    "teaching_reservations",
}

#: 租户级：只有租户谓词（跨项目聚合的账户）。
TENANT_SCOPED = {"teaching_tenant_budgets"}

#: append-only：事件流是"发生过什么"的账本，改写它等于篡改回放历史。
APPEND_ONLY = {"teaching_events"}

#: 状态要推进，但不该能删除历史。
NO_DELETE = {
    "teaching_runs",
    "provider_attempts",
    "teaching_budgets",
    "teaching_tenant_budgets",
    "teaching_reservations",
}

#: worker 的权限（契约生成器读这里）。`messages` 只有 INSERT：
#: worker 落定最终回答时插入一条消息；读取/改写历史消息都不需要。
WORKER_ROLE_GRANTS: dict[str, str] = {
    "teaching_runs": "SELECT, UPDATE",
    "provider_attempts": "SELECT, INSERT, UPDATE",
    "teaching_events": "SELECT, INSERT",
    "teaching_budgets": "SELECT, UPDATE",
    "teaching_tenant_budgets": "SELECT, UPDATE",
    "teaching_reservations": "SELECT, INSERT, UPDATE",
    "messages": "INSERT",
}


def _assert_roles_exist() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
        RAISE EXCEPTION 'role {WORKER_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql '
            '(it needs -v worker_password=... as well)';
    END IF;
END
$$
"""
    )


def _create_tables() -> list[str]:
    return [
        # 组合外键的父键（同 0007 为 sources 补键的做法）。
        """
ALTER TABLE conversations ADD CONSTRAINT conversations_scope_key
    UNIQUE (tenant_id, project_id, conversation_id)
""",
        """
ALTER TABLE messages ADD CONSTRAINT messages_scope_key
    UNIQUE (tenant_id, project_id, message_id)
""",
        # ---------------------------------------------------------------- 运行
        # question 冗余自 messages（append-only，副本稳定）：worker 构建上下文
        # 需要，不值得为此给 worker 开 messages 的 SELECT。
        """
CREATE TABLE teaching_runs (
    run_id          text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    conversation_id text NOT NULL,
    user_message_id text NOT NULL,
    principal_id    text NOT NULL REFERENCES principals (principal_id),
    answer_message_id text,
    answer_seq      bigint,
    question        text NOT NULL CHECK (octet_length(question) <= 65536),
    status          text NOT NULL CHECK (status IN
        ('queued', 'running', 'succeeded', 'failed', 'reconciliation_required')),
    attempt_count   integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claim_token     uuid,
    lease_owner     text,
    lease_until     timestamptz,
    model_id        text NOT NULL,
    prompt_version  text NOT NULL,
    ranking_version text NOT NULL,
    grounding       text NOT NULL DEFAULT ''
        CHECK (grounding IN ('', 'sourced', 'inference_only')),
    error_code      text NOT NULL DEFAULT '',
    error_detail    text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_message_id),
    UNIQUE (tenant_id, project_id, run_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, conversation_id)
        REFERENCES conversations (tenant_id, project_id, conversation_id),
    FOREIGN KEY (tenant_id, project_id, user_message_id)
        REFERENCES messages (tenant_id, project_id, message_id),
    FOREIGN KEY (tenant_id, project_id, answer_message_id)
        REFERENCES messages (tenant_id, project_id, message_id),
    CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL AND claim_token IS NOT NULL)),
    CHECK ((status IN ('failed', 'reconciliation_required')) = (error_code <> '')),
    CHECK ((status = 'succeeded') = (answer_message_id IS NOT NULL AND grounding <> ''))
)
""",
        # ------------------------------------------------------- provider 尝试
        # 一次运行至多一次派发（UNIQUE 约束兜底）：重试/重放复用同一个 attempt。
        # result_payload 是 provider 结果的持久化副本 —— final commit 失败后
        # 从这里重放落库，绝不重新调用模型。
        """
CREATE TABLE provider_attempts (
    attempt_id          text PRIMARY KEY,
    tenant_id           text NOT NULL REFERENCES tenants (tenant_id),
    project_id          text NOT NULL,
    run_id              text NOT NULL,
    status              text NOT NULL CHECK (status IN
        ('dispatched', 'unknown', 'completed', 'failed')),
    provider_request_id text NOT NULL DEFAULT '',
    result_payload      jsonb,
    input_tokens        integer,
    output_tokens       integer,
    cost_micro          bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, run_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, run_id)
        REFERENCES teaching_runs (tenant_id, project_id, run_id),
    CHECK ((status IN ('dispatched', 'unknown')) = (result_payload IS NULL))
)
""",
        # ---------------------------------------------------------------- 事件
        # (run_id, seq) 是 SSE 回放的游标：event id 来源于持久化序号，
        # 心跳与连接管理不产生业务事件。
        """
CREATE TABLE teaching_events (
    run_id     text NOT NULL,
    seq        integer NOT NULL CHECK (seq > 0),
    tenant_id  text NOT NULL REFERENCES tenants (tenant_id),
    project_id text NOT NULL,
    event_type text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, run_id)
        REFERENCES teaching_runs (tenant_id, project_id, run_id)
)
""",
        # ---------------------------------------------------------------- 预算
        # 租户级总额度：跨项目聚合的**物化**行 —— 现场对六张表做 SUM
        # 再判容量，会让"预留"变成跨行 check-then-act。
        """
CREATE TABLE teaching_tenant_budgets (
    tenant_id       text PRIMARY KEY REFERENCES tenants (tenant_id),
    total_micro     bigint NOT NULL CHECK (total_micro > 0),
    reserved_micro  bigint NOT NULL DEFAULT 0 CHECK (reserved_micro >= 0),
    in_flight_micro bigint NOT NULL DEFAULT 0 CHECK (in_flight_micro >= 0),
    spent_micro     bigint NOT NULL DEFAULT 0 CHECK (spent_micro >= 0),
    price_version   text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
)
""",
        """
CREATE TABLE teaching_budgets (
    tenant_id         text NOT NULL REFERENCES tenants (tenant_id),
    project_id        text NOT NULL,
    total_micro       bigint NOT NULL CHECK (total_micro > 0),
    max_input_tokens  integer NOT NULL CHECK (max_input_tokens > 0),
    max_output_tokens integer NOT NULL CHECK (max_output_tokens > 0),
    reserved_micro    bigint NOT NULL DEFAULT 0 CHECK (reserved_micro >= 0),
    in_flight_micro   bigint NOT NULL DEFAULT 0 CHECK (in_flight_micro >= 0),
    spent_micro       bigint NOT NULL DEFAULT 0 CHECK (spent_micro >= 0),
    price_version     text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id)
)
""",
        # 预留：held（已扣额度）→ in_flight（已派发）→ settled / released。
        # CHECK 把"实际费用"与状态绑死：settled 必须有 actual，
        # 其余状态必须没有 —— 不存在"结了账但预留还开着"的行。
        """
CREATE TABLE teaching_reservations (
    reservation_id          text PRIMARY KEY,
    tenant_id               text NOT NULL REFERENCES tenants (tenant_id),
    project_id              text NOT NULL,
    run_id                  text NOT NULL,
    state                   text NOT NULL CHECK (state IN
        ('held', 'in_flight', 'settled', 'released')),
    estimated_micro         bigint NOT NULL CHECK (estimated_micro > 0),
    estimated_input_tokens  integer NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens integer NOT NULL CHECK (estimated_output_tokens >= 0),
    actual_micro            bigint,
    price_version           text NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, run_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, run_id)
        REFERENCES teaching_runs (tenant_id, project_id, run_id),
    CHECK ((state = 'settled') = (actual_micro IS NOT NULL)),
    CHECK ((state IN ('held', 'in_flight', 'released')) = (actual_micro IS NULL))
)
""",
    ]


def _index_statements() -> list[str]:
    return [
        # 认领只看 queued 的运行（部分索引，同 0007 的队列索引）。
        "CREATE INDEX teaching_runs_claim_idx"
        " ON teaching_runs (created_at, run_id) WHERE status = 'queued'",
        "CREATE INDEX teaching_runs_scope_idx"
        " ON teaching_runs (tenant_id, project_id, conversation_id, created_at DESC)",
    ]


def _rls_statements(table: str, predicate: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _project_predicate() -> str:
    return (
        "tenant_id = current_setting('app.tenant_id', true)\n"
        "       AND project_id = current_setting('app.project_id', true)"
    )


def _tenant_predicate() -> str:
    return "tenant_id = current_setting('app.tenant_id', true)"


def _worker_policy() -> list[str]:
    """`teaching_runs` 的 worker 系统级可见性 —— 与 0008 的摄取队列同构。

    `TO study_worker` + `NULLIF` 谓词：凭据边界在**角色**上，
    自定义变量（app.worker_id）不是凭据。
    """
    predicate = "NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL"
    return [
        "DROP POLICY IF EXISTS teaching_runs_worker ON teaching_runs",
        "CREATE POLICY teaching_runs_worker ON teaching_runs"
        f"\n    TO {WORKER_ROLE}"
        f"\n    USING ({predicate})"
        f"\n    WITH CHECK ({predicate})",
    ]


def _grant_statements() -> list[str]:
    statements = [f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}"]
    for table in TABLES:
        if table in APPEND_ONLY:
            statements.append(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
        else:
            statements.append(
                f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}"
            )
    for table, privileges in WORKER_ROLE_GRANTS.items():
        statements.append(f"GRANT {privileges} ON {table} TO {WORKER_ROLE}")
    return statements


def upgrade() -> None:
    _assert_roles_exist()

    for statement in _create_tables():
        op.execute(statement)

    for table in TABLES:
        predicate = (
            _tenant_predicate() if table in TENANT_SCOPED else _project_predicate()
        )
        for statement in _rls_statements(table, predicate):
            op.execute(statement)

    for statement in _worker_policy():
        op.execute(statement)

    for statement in _index_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    """严格逆序，每一处都真的还原（含父键上的两个组合唯一约束）。"""
    op.execute("DROP INDEX IF EXISTS teaching_runs_scope_idx")
    op.execute("DROP INDEX IF EXISTS teaching_runs_claim_idx")

    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    op.execute("DROP POLICY IF EXISTS teaching_runs_worker ON teaching_runs")

    op.execute("REVOKE INSERT ON messages FROM study_worker")
    for table in WORKER_ROLE_GRANTS:
        if table == "messages":
            continue
        op.execute(f"REVOKE ALL ON {table} FROM {WORKER_ROLE}")
    op.execute("REVOKE ALL ON SCHEMA public FROM study_worker")

    op.execute("DROP TABLE IF EXISTS teaching_reservations CASCADE")
    op.execute("DROP TABLE IF EXISTS teaching_tenant_budgets CASCADE")
    op.execute("DROP TABLE IF EXISTS teaching_budgets CASCADE")
    op.execute("DROP TABLE IF EXISTS teaching_events CASCADE")
    op.execute("DROP TABLE IF EXISTS provider_attempts CASCADE")
    op.execute("DROP TABLE IF EXISTS teaching_runs CASCADE")

    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_scope_key")
    op.execute(
        "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_scope_key"
    )
