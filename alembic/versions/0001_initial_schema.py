"""干净初始基线：把 21 条历史迁移压成一条。

## 这条基线是什么

它是**原 head（0021）全部有效对象的合并快照**，不是"重写一套新结构"：

- 表、列、约束、索引、RLS 策略、`GRANT`、`SECURITY DEFINER` 函数、
  角色断言块 —— 逐条保留原链的**最终生效形态**；
- 语句的书写风格（在哪里用 `ALTER TABLE` 追加列、哪些表写成
  `CREATE TABLE public.<名字>`）刻意保持原样：契约生成器
  （`tools/skills/gen_contracts.py`）按 AST/正则解析迁移来推导
  `docs/skills/contracts/sql-schema.md`，改写法会让契约内容凭空漂移；
- 唯一的结构性删减是**邀请码全链路**（新版只保留用户名 + 密码）。

## 为什么压成一条

旧库不需要数据迁移（用户明确不需要旧数据）；21 条迁移里有大量
"建了又改、改了又还原"的中间态，留着只会让新库的语义难以阅读。
压扁后新库由**一条**迁移一次建成，单头 = `0001`。

## 确定性要求

`study_metrics_snapshot()` **只保留 0019 版**，且必须建在它引用的
全部列/表之后（`routing_decision` / `retrieval_decision` /
`provider_family` / `acquisition_fetch_observations`）。

## 运行时约定（原样保留）

- 所有 definer 函数 `SET search_path = pg_catalog`，表名全限定；
- RLS 谓词一律 `current_setting(..., true)`（缺上下文 → NULL → 零行）；
- worker 三条策略 `TO study_worker` + `NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL`；
- `REVOKE ALL ON SCHEMA public FROM PUBLIC`、各表 `GRANT`、
  `GRANT SELECT ON alembic_version TO study_app`；
- 迁移不建 role、不建 extension；角色不存在时**显式失败**。

## downgrade

基线 `downgrade` 只在**没有业务数据**时才允许清库：任何业务表还有行
一律拒绝（`platform_budget_config` 的种子行不算业务数据，升级会重建它）。
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "study_app"
WORKER_ROLE = "study_worker"

#: 会话期限硬上限（天）。必须与 app/identity/limits.py::MAX_SESSION_TTL 一致。
MAX_SESSION_TTL_DAYS = 30

#: 注册时自动创建的默认项目名（与 0012 的 `register_account` 一致）。
DEFAULT_PROJECT_NAME = "我的学习项目"

#: `ingestion_jobs` 的认领围栏列由 f-string 追加（契约生成器的占位符规则）。
TABLE = "ingestion_jobs"

# --------------------------------------------------------------------- 契约声明
#
# 下面这些常量是**给契约生成器读的人工声明**（真正生效的是数据库里的
# pg_policy 与 information_schema.table_privileges）。压扁后它们汇总成
# 原链的最终状态：后续迁移的声明覆盖先前迁移，这里直接写最终值。

#: 系统级表：无租户、无表权限，仅 SECURITY DEFINER 函数可触达。
SYSTEM_TABLES = ["auth_attempt_counters"]

#: 项目级表：读写上下文必须同时含 tenant 与 project。
PROJECT_SCOPED = {
    "acquisition_jobs",
    "action_intents",
    "confirmations",
    "conversations",
    "diagnoses",
    "evidence_events",
    "ingestion_jobs",
    "learning_plans",
    "learning_tasks",
    "messages",
    "milestones",
    "provider_attempts",
    "source_candidates",
    "source_chunks",
    "source_documents",
    "sources",
    "task_assessments",
    "task_submissions",
    "teaching_budgets",
    "teaching_events",
    "teaching_reservations",
    "teaching_runs",
}

#: 主体级表：还要求 `app.principal_id`。
PRINCIPAL_SCOPED = {
    "diagnoses",
    "http_idempotency",
    "library_documents",
    "library_sources",
    "task_submissions",
    "user_sessions",
}

#: append-only：应用角色只有 SELECT / INSERT。
APPEND_ONLY = {
    "diagnoses",
    "evidence_events",
    "library_documents",
    "library_sources",
    "messages",
    "source_chunks",
    "source_documents",
    "task_assessments",
    "task_submissions",
    "teaching_events",
}

#: 只给 SELECT / INSERT / UPDATE：状态要推进，但不该能删除历史。
NO_DELETE = {
    "acquisition_jobs",
    "auth_audit_outbox",
    "http_idempotency",
    "ingestion_jobs",
    "provider_attempts",
    "source_candidates",
    "teaching_budgets",
    "teaching_reservations",
    "teaching_runs",
    "teaching_tenant_budgets",
}

#: 改写既有表隔离级别的声明（键是表名，值是契约里的隔离级别文字）。
POLICY_OVERRIDES: dict[str, str] = {
    "projects": "成员感知",
    "auth_audit_outbox": "系统级（认证前审计事实中转；应用 INSERT/SELECT/UPDATE，无 DELETE）",
}

#: 收回应用角色权限的表及其新权限集。
APP_ROLE_GRANTS_OVERRIDES: dict[str, str] = {
    "ingestion_jobs": "SELECT, INSERT",
    "acquisition_jobs": "SELECT, INSERT",
}

#: worker 角色的权限（另一条凭据边界）。
WORKER_ROLE_GRANTS: dict[str, str] = {
    "acquisition_jobs": "SELECT, UPDATE",
    "ingestion_jobs": "SELECT, UPDATE",
    "messages": "INSERT",
    "provider_attempts": "SELECT, INSERT, UPDATE",
    "source_candidates": "SELECT, INSERT, UPDATE",
    "source_chunks": "SELECT, INSERT",
    "source_documents": "SELECT",
    "teaching_budgets": "SELECT, UPDATE",
    "teaching_events": "SELECT, INSERT",
    "teaching_reservations": "SELECT, INSERT, UPDATE",
    "teaching_runs": "SELECT, UPDATE",
    "teaching_tenant_budgets": "SELECT, UPDATE",
}


# --------------------------------------------------------------------- 角色断言


def _assert_roles_exist() -> None:
    """两个角色都必须存在，否则显式失败。

    跳过 `GRANT` 会得到"迁移成功、应用却什么都没权限"的中间态，
    那比失败更难排查；所以这里 0001-0007 的单角色断言收敛为两者的断言。
    """
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
        RAISE EXCEPTION 'role {WORKER_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql '
            '(it needs -v worker_password=... as well)';
    END IF;
END
$$
"""
    )


# --------------------------------------------------------------------- 建表


def _core_tables() -> list[str]:
    """核心表（原 0001）。"""
    return [
        """
CREATE TABLE tenants (
    tenant_id  text PRIMARY KEY,
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
""",
        """
CREATE TABLE principals (
    principal_id text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
    display_name text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
)
""",
        """
CREATE TABLE projects (
    project_id text PRIMARY KEY,
    tenant_id  text NOT NULL REFERENCES tenants (tenant_id),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
""",
        """
CREATE TABLE project_grants (
    tenant_id    text NOT NULL,
    principal_id text NOT NULL,
    project_id   text NOT NULL REFERENCES projects (project_id),
    granted_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, project_id)
)
""",
        """
CREATE TABLE confirmations (
    confirmation_id  text PRIMARY KEY,
    tenant_id        text NOT NULL,
    project_id       text NOT NULL REFERENCES projects (project_id),
    principal_id     text NOT NULL,
    tool_id          text NOT NULL,
    params_hash      text NOT NULL,
    ceiling_dimension text NOT NULL,
    ceiling_amount   bigint NOT NULL CHECK (ceiling_amount >= 0),
    ceiling_note     text NOT NULL DEFAULT '',
    issued_at        timestamptz NOT NULL,
    expires_at       timestamptz NOT NULL,
    consumed_at      timestamptz,
    CHECK (expires_at > issued_at)
)
""",
        """
CREATE TABLE evidence_events (
    seq            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    event_id       text NOT NULL UNIQUE,
    tenant_id      text NOT NULL,
    project_id     text NOT NULL REFERENCES projects (project_id),
    kind           text NOT NULL,
    task_id        text NOT NULL,
    contract_id    text,
    mapping_version text,
    graph_version  text NOT NULL,
    occurred_at    timestamptz NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    payload        jsonb NOT NULL
)
""",
        """
CREATE TABLE action_intents (
    logical_action_id text PRIMARY KEY,
    tenant_id         text NOT NULL,
    project_id        text NOT NULL REFERENCES projects (project_id),
    run_id            text NOT NULL,
    node_instance_id  text NOT NULL,
    tool_id           text NOT NULL,
    idempotency_key   text NOT NULL UNIQUE,
    state             text NOT NULL,
    attempts          jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
)
""",
    ]


def _product_tables() -> list[str]:
    """产品底座（原 0002，去掉 `invitations`）。"""
    return [
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


def _learning_loop_tables() -> list[str]:
    """学习闭环（原 0005）。"""
    return [
        # 0003 只给 principals / projects 建了组合唯一键；learning_tasks 的
        # 组合唯一键在这里补 —— 它是下面三列组合外键的父键。
        """
ALTER TABLE learning_tasks ADD CONSTRAINT learning_tasks_scope_key
    UNIQUE (tenant_id, project_id, task_id)
""",
        """
CREATE TABLE task_assessments (
    assessment_id   text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    task_id         text NOT NULL,
    component_id    text NOT NULL,
    contract_id     text NOT NULL,
    mapping_version text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, task_id)
        REFERENCES learning_tasks (tenant_id, project_id, task_id)
)
""",
        """
CREATE TABLE task_submissions (
    submission_id text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    project_id    text NOT NULL,
    task_id       text NOT NULL,
    principal_id  text NOT NULL,
    mode          text NOT NULL CHECK (mode IN ('self_report')),
    content       text NOT NULL CHECK (length(content) <= 20000),
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, task_id)
        REFERENCES learning_tasks (tenant_id, project_id, task_id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES principals (tenant_id, principal_id)
)
""",
        """
CREATE TABLE diagnoses (
    diagnosis_id text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id),
    project_id   text NOT NULL,
    principal_id text NOT NULL,
    answers      jsonb NOT NULL,
    summary      text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES principals (tenant_id, principal_id)
)
""",
    ]


def _audit_tables() -> list[str]:
    """认证审计 transactional outbox（原 0006）。"""
    return [
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
""",
    ]


def _ingestion_tables() -> list[str]:
    """资料摄取（原 0007）。"""
    return [
        # 0002 只给 sources 建了 (project_id, identity_hash) 唯一键；
        # (tenant_id, project_id, source_id) 是下面组合外键的父键，在这里补。
        """
ALTER TABLE sources ADD CONSTRAINT sources_scope_key
    UNIQUE (tenant_id, project_id, source_id)
""",
        """
CREATE TABLE source_documents (
    document_id        text PRIMARY KEY,
    tenant_id          text NOT NULL REFERENCES tenants (tenant_id),
    project_id         text NOT NULL,
    source_id          text NOT NULL,
    version            integer NOT NULL CHECK (version > 0),
    document_title     text NOT NULL,
    content            text NOT NULL
        CHECK (octet_length(content) <= 1048576),
    content_hash       text NOT NULL,
    media_type         text NOT NULL
        CHECK (media_type IN ('text/plain', 'text/markdown')),
    language           text NOT NULL,
    parser_version     text NOT NULL,
    acquisition_method text NOT NULL,
    taint_sources      jsonb NOT NULL DEFAULT '[]'::jsonb,
    derived_from       jsonb NOT NULL DEFAULT '[]'::jsonb,
    observed_at        timestamptz NOT NULL,
    UNIQUE (source_id, version),
    UNIQUE (tenant_id, project_id, document_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id)
)
""",
        """
CREATE TABLE ingestion_jobs (
    job_id        text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    project_id    text NOT NULL,
    source_id     text NOT NULL,
    document_id   text NOT NULL,
    status        text NOT NULL
        CHECK (status IN ('queued', 'processing', 'succeeded', 'failed')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner   text,
    lease_until   timestamptz,
    error_code    text NOT NULL DEFAULT '',
    error_detail  text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, document_id)
        REFERENCES source_documents (tenant_id, project_id, document_id),
    CHECK ((status = 'processing') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code <> ''))
)
""",
        """
CREATE TABLE source_chunks (
    chunk_id       text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
    project_id     text NOT NULL,
    source_id      text NOT NULL,
    document_id    text NOT NULL,
    chunk_index    integer NOT NULL CHECK (chunk_index >= 0),
    heading_path   jsonb NOT NULL DEFAULT '[]'::jsonb,
    heading_level  integer NOT NULL DEFAULT 0 CHECK (heading_level >= 0),
    span_start     integer NOT NULL,
    span_end       integer NOT NULL,
    content        text NOT NULL,
    content_hash   text NOT NULL,
    parser_version text NOT NULL,
    display_policy text NOT NULL DEFAULT 'full'
        CHECK (display_policy IN ('full', 'summary', 'citation_only')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index),
    CHECK (span_start >= 0 AND span_end > span_start),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, document_id)
        REFERENCES source_documents (tenant_id, project_id, document_id)
)
""",
    ]


def _teaching_tables() -> list[str]:
    """模型教学交互（原 0010）。"""
    return [
        """
ALTER TABLE conversations ADD CONSTRAINT conversations_scope_key
    UNIQUE (tenant_id, project_id, conversation_id)
""",
        """
ALTER TABLE messages ADD CONSTRAINT messages_scope_key
    UNIQUE (tenant_id, project_id, message_id)
""",
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
    status          text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'reconciliation_required')),
    attempt_count   integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claim_token     uuid,
    lease_owner     text,
    lease_until     timestamptz,
    model_id        text NOT NULL,
    prompt_version  text NOT NULL,
    ranking_version text NOT NULL,
    grounding       text NOT NULL DEFAULT '' CHECK (grounding IN ('', 'sourced', 'inference_only')),
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
        """
CREATE TABLE provider_attempts (
    attempt_id          text PRIMARY KEY,
    tenant_id           text NOT NULL REFERENCES tenants (tenant_id),
    project_id          text NOT NULL,
    run_id              text NOT NULL,
    status              text NOT NULL CHECK (status IN ('dispatched', 'unknown', 'completed', 'failed')),
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
        """
CREATE TABLE teaching_reservations (
    reservation_id          text PRIMARY KEY,
    tenant_id               text NOT NULL REFERENCES tenants (tenant_id),
    project_id              text NOT NULL,
    run_id                  text NOT NULL,
    state                   text NOT NULL CHECK (state IN ('held', 'in_flight', 'settled', 'released')),
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


def _credential_tables() -> list[str]:
    """开放注册凭据与平台额度（原 0012；表名带 `public.` 前缀，见模块说明）。"""
    return [
        """
CREATE TABLE public.platform_budget_config (
    config_id boolean PRIMARY KEY DEFAULT true CHECK (config_id),
    monthly_cap_micro bigint NOT NULL DEFAULT 0 CHECK (monthly_cap_micro >= 0),
    paid_dispatch_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
)
""",
        """
CREATE TABLE public.platform_paid_reservations (
    reservation_id text PRIMARY KEY,
    billing_period date NOT NULL,
    provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 100),
    price_version text NOT NULL CHECK (char_length(price_version) BETWEEN 1 AND 100),
    max_spend_micro bigint NOT NULL CHECK (max_spend_micro > 0),
    state text NOT NULL CHECK (state IN ('held','in_flight','settled','released')),
    actual_spend_micro bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    CONSTRAINT platform_paid_actual_check
        CHECK (actual_spend_micro IS NULL OR (actual_spend_micro >= 0 AND actual_spend_micro <= max_spend_micro)),
    CONSTRAINT platform_paid_settled_fields_check
        CHECK ((state = 'settled') = (actual_spend_micro IS NOT NULL))
)
""",
        """
CREATE TABLE public.account_credentials (
    credential_id       text PRIMARY KEY,
    tenant_id           text NOT NULL,
    principal_id        text NOT NULL,
    username            text NOT NULL,
    username_normalized text COLLATE "C" NOT NULL,
    password_hash       text NOT NULL,
    hash_version        integer NOT NULL,
    security_generation bigint NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    last_login_at       timestamptz,
    disabled_at         timestamptz,
    CONSTRAINT account_credentials_username_length_check
        CHECK (char_length(username) BETWEEN 1 AND 16),
    CONSTRAINT account_credentials_normalized_length_check
        CHECK (char_length(username_normalized) BETWEEN 1 AND 16),
    CONSTRAINT account_credentials_password_hash_check
        CHECK (password_hash LIKE '$argon2id$%'),
    CONSTRAINT account_credentials_hash_version_check
        CHECK (hash_version > 0),
    CONSTRAINT account_credentials_security_generation_check
        CHECK (security_generation > 0),
    CONSTRAINT account_credentials_disabled_time_check
        CHECK (disabled_at IS NULL OR disabled_at >= created_at),
    CONSTRAINT account_credentials_tenant_principal_fk
        FOREIGN KEY (tenant_id, principal_id)
        REFERENCES public.principals (tenant_id, principal_id),
    CONSTRAINT account_credentials_tenant_principal_uq
        UNIQUE (tenant_id, principal_id),
    CONSTRAINT account_credentials_tenant_principal_credential_uq
        UNIQUE (tenant_id, principal_id, credential_id)
)
""",
    ]


def _acquisition_tables() -> list[str]:
    """显式外部抓取候选与任务（原 0014）。"""
    return [
        """
CREATE TABLE source_candidates (
    candidate_id    text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    url             text NOT NULL,
    title           text NOT NULL,
    snippet         text NOT NULL DEFAULT '',
    source_domain   text NOT NULL,
    status          text NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'selected', 'rejected', 'expired')),
    discovered_at   timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    UNIQUE (tenant_id, project_id, candidate_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    CHECK (expires_at > discovered_at)
)
""",
        """
CREATE TABLE acquisition_jobs (
    acquisition_id  text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    source_id       text NOT NULL,
    candidate_id    text NOT NULL,
    requested_by    text NOT NULL,
    url             text NOT NULL,
    title           text NOT NULL,
    media_type      text NOT NULL
        CHECK (media_type IN ('text/plain', 'text/markdown', 'text/html')),
    language        text NOT NULL,
    idempotency_key text NOT NULL,
    status          text NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'unknown')),
    attempt_count   integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner     text,
    lease_until     timestamptz,
    claim_token     uuid,
    error_code      text NOT NULL DEFAULT '',
    error_detail    text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, idempotency_key),
    UNIQUE (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, candidate_id)
        REFERENCES source_candidates (tenant_id, project_id, candidate_id),
    CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL AND claim_token IS NOT NULL)),
    CHECK ((status IN ('failed', 'unknown')) = (error_code <> ''))
)
""",
    ]


def _artifact_tables() -> list[str]:
    """不可变抓取字节（原 0016）。"""
    return [
        """
CREATE TABLE source_fetch_artifacts (
    acquisition_id text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
    project_id     text NOT NULL,
    content_type   text NOT NULL
        CHECK (content_type IN ('text/html', 'text/markdown', 'text/plain')),
    raw_content    bytea NOT NULL
        CHECK (octet_length(raw_content) BETWEEN 1 AND 1048576),
    content_hash   text NOT NULL
        CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    parser_version text NOT NULL
        CHECK (parser_version IN ('html-to-markdown/v1', 'web-text/v1')),
    fetched_at     timestamptz NOT NULL,
    UNIQUE (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id, acquisition_id)
        REFERENCES acquisition_jobs (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id)
)
""",
    ]


def _metrics_tables() -> list[str]:
    """按次抓取观测（原 0018；表名带 `public.` 前缀）。"""
    return [
        """
CREATE TABLE public.acquisition_fetch_observations (
    tenant_id          text NOT NULL,
    project_id         text NOT NULL,
    acquisition_id     text NOT NULL,
    attempt_number     integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    outcome            text NOT NULL DEFAULT 'unknown'
        CHECK (outcome IN ('succeeded', 'failed', 'unknown')),
    duration_seconds   double precision,
    response_body_bytes bigint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, acquisition_id, attempt_number),
    FOREIGN KEY (tenant_id, project_id, acquisition_id)
        REFERENCES public.acquisition_jobs (tenant_id, project_id, acquisition_id),
    CHECK (duration_seconds IS NULL OR duration_seconds BETWEEN 0 AND 86400),
    CHECK (response_body_bytes IS NULL OR response_body_bytes BETWEEN 0 AND 1048576),
    CHECK (response_body_bytes IS NULL OR duration_seconds IS NOT NULL),
    CHECK (
        duration_seconds IS NOT NULL
        OR (outcome = 'unknown' AND response_body_bytes IS NULL)
    )
)
""",
    ]


def _library_tables() -> list[str]:
    """用户级共享知识库（原 0021）。"""
    return [
        """
CREATE TABLE library_sources (
    library_source_id text PRIMARY KEY,
    tenant_id         text NOT NULL REFERENCES tenants (tenant_id),
    principal_id      text NOT NULL,
    display_name      text NOT NULL,
    media_type        text NOT NULL DEFAULT '',
    identity_hash     text NOT NULL,
    acquisition       jsonb NOT NULL DEFAULT '{}'::jsonb,
    registered_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, principal_id, identity_hash),
    UNIQUE (tenant_id, principal_id, library_source_id)
)
""",
        """
CREATE TABLE library_documents (
    library_document_id text PRIMARY KEY,
    tenant_id           text NOT NULL REFERENCES tenants (tenant_id),
    principal_id        text NOT NULL,
    library_source_id   text NOT NULL,
    version             integer NOT NULL CHECK (version > 0),
    content             text NOT NULL
        CHECK (octet_length(content) <= 1048576),
    content_hash        text NOT NULL,
    parser_version      text NOT NULL,
    observed_at         timestamptz NOT NULL,
    UNIQUE (library_source_id, version),
    FOREIGN KEY (tenant_id, principal_id, library_source_id)
        REFERENCES library_sources (tenant_id, principal_id, library_source_id)
)
""",
    ]


# --------------------------------------------------------------------- 组合外键


def _unique_parent_keys() -> list[str]:
    """组合外键的目标必须先存在：父表要有 (tenant_id, 键) 的唯一约束。"""
    return [
        "ALTER TABLE principals"
        " ADD CONSTRAINT principals_tenant_principal_uq UNIQUE (tenant_id, principal_id)",
        "ALTER TABLE projects"
        " ADD CONSTRAINT projects_tenant_project_uq UNIQUE (tenant_id, project_id)",
    ]


#: 直接引用 `projects` 的表：单列外键 → (tenant_id, project_id) 组合外键。
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

#: 引用 `principals` 的 (租户, 主体) 组合外键。
PRINCIPAL_REFERENCES: dict[str, list[str]] = {
    "user_sessions": ["principal_id"],
    "http_idempotency": ["principal_id"],
}


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
    statements.append(
        "ALTER TABLE http_idempotency ADD CONSTRAINT http_idempotency_tenant_project_fk"
        " FOREIGN KEY (tenant_id, project_id)"
        " REFERENCES projects (tenant_id, project_id)"
    )
    return statements


# --------------------------------------------------------------------- 索引


def _core_indexes() -> list[str]:
    return [
        "CREATE INDEX confirmations_expiry_idx ON confirmations (tenant_id, expires_at)",
        "CREATE INDEX confirmations_live_idx ON confirmations (confirmation_id)"
        " WHERE consumed_at IS NULL",
        "CREATE INDEX evidence_events_project_seq_idx ON evidence_events (project_id, seq)",
        "CREATE INDEX action_intents_state_idx ON action_intents (tenant_id, project_id, state)",
    ]


def _product_indexes() -> list[str]:
    return [
        "CREATE INDEX project_grants_principal_idx ON project_grants (principal_id, project_id)",
        "CREATE INDEX user_sessions_expiry_idx ON user_sessions (tenant_id, expires_at)",
        "CREATE INDEX user_sessions_live_idx ON user_sessions (session_id)"
        " WHERE revoked_at IS NULL",
        "CREATE INDEX messages_conversation_seq_idx ON messages (conversation_id, seq)",
        "CREATE INDEX learning_plans_project_version_idx"
        " ON learning_plans (project_id, version DESC)",
        "CREATE INDEX sources_project_idx ON sources (project_id, registered_at)",
        "CREATE INDEX http_idempotency_claim_age_idx ON http_idempotency (claimed_at)",
    ]


def _learning_loop_indexes() -> list[str]:
    return [
        "CREATE INDEX task_submissions_task_idx ON task_submissions (task_id, created_at)",
        "CREATE INDEX task_assessments_task_idx ON task_assessments (task_id)",
    ]


def _ingestion_indexes() -> list[str]:
    return [
        "CREATE INDEX source_documents_scope_idx"
        " ON source_documents (tenant_id, project_id, observed_at DESC)",
        "CREATE INDEX ingestion_jobs_claim_idx"
        " ON ingestion_jobs (created_at, job_id)"
        " WHERE status IN ('queued', 'processing')",
        "CREATE INDEX source_chunks_scope_idx"
        " ON source_chunks (tenant_id, project_id, source_id, chunk_index)",
    ]


def _teaching_indexes() -> list[str]:
    return [
        "CREATE INDEX teaching_runs_claim_idx"
        " ON teaching_runs (created_at, run_id) WHERE status = 'queued'",
        "CREATE INDEX teaching_runs_scope_idx"
        " ON teaching_runs (tenant_id, project_id, conversation_id, created_at DESC)",
    ]


def _acquisition_indexes() -> list[str]:
    return [
        "CREATE INDEX source_candidates_scope_idx"
        " ON source_candidates (tenant_id, project_id, discovered_at DESC)",
        "CREATE INDEX acquisition_jobs_claim_idx"
        " ON acquisition_jobs (created_at, acquisition_id)"
        " WHERE status IN ('queued', 'running')",
    ]


def _metrics_indexes() -> list[str]:
    return [
        "CREATE INDEX acquisition_fetch_observations_aggregate_idx"
        " ON public.acquisition_fetch_observations (outcome)"
        " INCLUDE (duration_seconds, response_body_bytes)",
    ]


def _library_indexes() -> list[str]:
    return [
        "CREATE INDEX library_sources_owner_idx"
        " ON library_sources (tenant_id, principal_id, registered_at)",
    ]


# --------------------------------------------------------------------- RLS


def _tenant_project_predicate(*, principal: bool = False) -> str:
    parts = [
        "tenant_id = current_setting('app.tenant_id', true)",
        "project_id = current_setting('app.project_id', true)",
    ]
    if principal:
        parts.append("principal_id = current_setting('app.principal_id', true)")
    return "\n       AND ".join(parts)


def _tenant_principal_predicate() -> str:
    return (
        "tenant_id = current_setting('app.tenant_id', true)\n"
        "       AND principal_id = current_setting('app.principal_id', true)"
    )


def _rls_statements(table: str, predicate: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _tenant_predicate() -> str:
    return "tenant_id = current_setting('app.tenant_id', true)"


def _projects_membership_policy() -> list[str]:
    """`projects` 的策略：成员感知版（读靠成员关系，写靠租户上下文）。"""
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


def _worker_policy(table: str) -> list[str]:
    """worker 的系统级可见性：凭据边界在**角色**上，`app.worker_id` 不是凭据。

    `FOR ALL`（默认）是必需的：认领走 `SELECT ... FOR UPDATE`，
    它同时要求行通过 SELECT 与 UPDATE 两条策略。
    """
    predicate = "NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL"
    return [
        f"DROP POLICY IF EXISTS {table}_worker ON {table}",
        f"CREATE POLICY {table}_worker ON {table}"
        f"\n    TO {WORKER_ROLE}"
        f"\n    USING ({predicate})"
        f"\n    WITH CHECK ({predicate})",
    ]


# --------------------------------------------------------------------- 授权


def _app_grants() -> list[str]:
    """应用角色在各表上的权限（与原链最终状态一致）。"""
    statements: list[str] = [
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    ]
    tables = (
        "tenants",
        "principals",
        "projects",
        "project_grants",
        "confirmations",
        "evidence_events",
        "action_intents",  # ---- core
        "user_sessions",
        "conversations",
        "messages",
        "learning_plans",
        "milestones",
        "learning_tasks",
        "sources",
        "http_idempotency",  # ---- product
        "task_assessments",
        "task_submissions",
        "diagnoses",  # ---- learning loop
        "auth_audit_outbox",  # ---- audit
        "source_documents",
        "ingestion_jobs",
        "source_chunks",  # ---- ingestion
        "teaching_runs",
        "provider_attempts",
        "teaching_events",
        "teaching_budgets",
        "teaching_tenant_budgets",
        "teaching_reservations",  # ---- teaching
        "source_candidates",
        "acquisition_jobs",  # ---- acquisition
        "library_sources",
        "library_documents",  # ---- library
    )
    for table in tables:
        if table in APPEND_ONLY:
            privileges = "SELECT, INSERT"
        elif table in NO_DELETE:
            privileges = "SELECT, INSERT, UPDATE"
        else:
            privileges = "SELECT, INSERT, UPDATE, DELETE"
        if table in APP_ROLE_GRANTS_OVERRIDES:
            privileges = APP_ROLE_GRANTS_OVERRIDES[table]
        statements.append(f"GRANT {privileges} ON {table} TO {APP_ROLE}")
    # identity 列的序列需要 USAGE 才能 INSERT。
    statements.append(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # 启动自检需要读迁移版本。
    statements.append(f"GRANT SELECT ON alembic_version TO {APP_ROLE}")
    return statements


def _audit_outbox_grants() -> list[str]:
    return [f"GRANT INSERT, SELECT, UPDATE ON auth_audit_outbox TO {APP_ROLE}"]


def _worker_grants() -> list[str]:
    statements = [f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}"]
    for table, privileges in WORKER_ROLE_GRANTS.items():
        statements.append(f"GRANT {privileges} ON {table} TO {WORKER_ROLE}")
    return statements


def _credential_revocations() -> list[str]:
    """凭据/平台/系统表的权限面：应用与 worker 都拿不到裸表权限。"""
    statements: list[str] = []
    for table in (
        "platform_budget_config",
        "platform_paid_reservations",
        "account_credentials",
    ):
        statements.append(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        statements.append(f"REVOKE ALL ON TABLE public.{table} FROM {APP_ROLE}")
        statements.append(f"REVOKE ALL ON TABLE public.{table} FROM {WORKER_ROLE}")
    statements.append("REVOKE ALL ON auth_attempt_counters FROM PUBLIC")
    statements.append(f"REVOKE ALL ON auth_attempt_counters FROM {APP_ROLE}")
    statements.append(
        "REVOKE ALL ON public.acquisition_fetch_observations FROM PUBLIC, study_app"
    )
    statements.append(
        "GRANT SELECT, INSERT, UPDATE ON public.acquisition_fetch_observations"
        " TO study_worker"
    )
    statements.append("REVOKE ALL ON source_fetch_artifacts FROM PUBLIC")
    statements.append("REVOKE ALL ON source_fetch_artifacts FROM study_app")
    statements.append("GRANT SELECT, INSERT ON source_fetch_artifacts TO study_worker")
    return statements


# --------------------------------------------------------------------- 函数


def _rate_limit_function() -> str:
    """限流计数（0012 形态：schema 全限定 + 机会式清理 + 硬桶上限）。"""
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
    v_attempts integer;
BEGIN
    IF p_bucket IS NULL OR length(p_bucket) = 0 OR p_window_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid rate limit arguments' USING ERRCODE = 'check_violation';
    END IF;
    v_window_start := to_timestamp(
        floor(extract(epoch from clock_timestamp()) / p_window_seconds) * p_window_seconds
    );
    DELETE FROM public.auth_attempt_counters
     WHERE window_start < now() - interval '24 hours';
    IF (SELECT count(*) FROM public.auth_attempt_counters) >= 10000
       AND NOT EXISTS (SELECT 1 FROM public.auth_attempt_counters WHERE bucket = p_bucket)
    THEN
        RETURN 2147483647;
    END IF;
    INSERT INTO public.auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
    VALUES (p_bucket, v_window_start, 1, clock_timestamp())
    ON CONFLICT (bucket) DO UPDATE
       SET window_start = CASE WHEN c.window_start < EXCLUDED.window_start
                               THEN EXCLUDED.window_start ELSE c.window_start END,
           attempts = CASE WHEN c.window_start < EXCLUDED.window_start
                           THEN 1 ELSE c.attempts + 1 END,
           last_at = EXCLUDED.last_at
    RETURNING c.attempts INTO v_attempts;
    RETURN v_attempts;
END;
$fn$;
"""


def _audit_pending_count_function() -> str:
    return """
CREATE OR REPLACE FUNCTION public.auth_audit_pending_count()
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
    SELECT count(*) FROM public.auth_audit_outbox WHERE projected_at IS NULL
$fn$
"""


def _invalidate_credential_sessions_function() -> str:
    """凭据失效触发器：与直接 UPDATE 同一事务，撤销该凭据的全部会话。"""
    return """
CREATE OR REPLACE FUNCTION public.invalidate_credential_sessions()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
BEGIN
    IF NEW.security_generation <> OLD.security_generation
       OR (OLD.disabled_at IS NULL AND NEW.disabled_at IS NOT NULL)
    THEN
        UPDATE public.user_sessions
           SET revoked_at = COALESCE(revoked_at, now())
         WHERE credential_id = NEW.credential_id
           AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END;
$fn$;
"""


def _reserve_platform_paid_function() -> str:
    """平台额度预留（0013 形态：`monthly_cap_micro` 可为 NULL = 不限月度金额）。"""
    row_type = (
        "reservation_id text, billing_period date, provider text, "
        "price_version text, max_spend_micro bigint, state text, "
        "actual_spend_micro bigint"
    )
    return f"""
CREATE OR REPLACE FUNCTION public.reserve_platform_paid(
    p_reservation_id text,
    p_billing_period date,
    p_provider text,
    p_price_version text,
    p_max_spend_micro bigint
)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_cap bigint;
    v_enabled boolean;
    v_exposure numeric;
BEGIN
    IF p_reservation_id IS NULL OR char_length(p_reservation_id) = 0
       OR p_billing_period IS NULL
       OR p_billing_period <> date_trunc('month', now() AT TIME ZONE 'UTC')::date
       OR p_provider IS NULL OR char_length(p_provider) NOT BETWEEN 1 AND 100
       OR p_price_version IS NULL OR char_length(p_price_version) NOT BETWEEN 1 AND 100
       OR p_max_spend_micro IS NULL OR p_max_spend_micro <= 0
    THEN
        RAISE EXCEPTION 'invalid platform reservation arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT monthly_cap_micro, paid_dispatch_enabled
      INTO v_cap, v_enabled
      FROM public.platform_budget_config
     WHERE config_id = true
     FOR UPDATE;
    IF NOT FOUND OR NOT v_enabled THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.billing_period = p_billing_period
       AND r.provider = p_provider
       AND r.price_version = p_price_version
       AND r.max_spend_micro = p_max_spend_micro
       AND r.state IN ('held', 'in_flight');
    IF FOUND THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.platform_paid_reservations AS r
         WHERE r.reservation_id = p_reservation_id
    ) THEN
        RAISE EXCEPTION 'platform reservation idempotency conflict'
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_cap IS NOT NULL THEN
        SELECT COALESCE(SUM(
                   CASE WHEN r.state = 'settled' AND r.billing_period = p_billing_period
                        THEN r.actual_spend_micro ELSE 0 END
               ), 0)
             + COALESCE(SUM(
                   CASE WHEN r.state = 'held' AND r.billing_period = p_billing_period
                        THEN r.max_spend_micro ELSE 0 END
               ), 0)
             + COALESCE(SUM(
                   CASE WHEN r.state = 'in_flight' THEN r.max_spend_micro ELSE 0 END
               ), 0)
          INTO v_exposure
          FROM public.platform_paid_reservations AS r;
        IF v_exposure + p_max_spend_micro > v_cap THEN
            RETURN;
        END IF;
    END IF;

    INSERT INTO public.platform_paid_reservations (
        reservation_id, billing_period, provider, price_version,
        max_spend_micro, state
    ) VALUES (
        p_reservation_id, p_billing_period, p_provider, p_price_version,
        p_max_spend_micro, 'held'
    )
    RETURNING platform_paid_reservations.reservation_id,
              platform_paid_reservations.billing_period,
              platform_paid_reservations.provider,
              platform_paid_reservations.price_version,
              platform_paid_reservations.max_spend_micro,
              platform_paid_reservations.state,
              platform_paid_reservations.actual_spend_micro
         INTO reservation_id, billing_period, provider, price_version,
              max_spend_micro, state, actual_spend_micro;
    RETURN NEXT;
END;
$fn$;
"""


def _platform_transition_functions() -> list[str]:
    row_type = (
        "reservation_id text, billing_period date, provider text, "
        "price_version text, max_spend_micro bigint, state text, "
        "actual_spend_micro bigint"
    )
    return [
        f"""
CREATE OR REPLACE FUNCTION public.mark_platform_paid_in_flight(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'in_flight'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'held'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.release_platform_paid_held(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'released';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'released'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'held'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.release_platform_paid_failed(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'released';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'released'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.settle_platform_paid(
    p_reservation_id text,
    p_actual_spend_micro bigint
)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    IF p_actual_spend_micro IS NULL OR p_actual_spend_micro < 0 THEN
        RAISE EXCEPTION 'platform usage is unknown or negative'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'settled'
       AND r.actual_spend_micro = p_actual_spend_micro;
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'settled', actual_spend_micro = p_actual_spend_micro,
           settled_at = now()
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight'
       AND p_actual_spend_micro <= r.max_spend_micro
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'platform reservation cannot be settled'
            USING ERRCODE = 'check_violation';
    END IF;
END;
$fn$;
""",
    ]


def _register_account_function() -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.register_account(
    p_username text,
    p_username_normalized text,
    p_password_hash text,
    p_hash_version integer,
    p_session_expires_at timestamptz
)
RETURNS TABLE (
    credential_id text,
    session_id text,
    tenant_id text,
    principal_id text,
    expires_at timestamptz,
    security_generation bigint,
    default_project_id text,
    default_project_name text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_tenant_id text := 't_' || replace(gen_random_uuid()::text, '-', '');
    v_principal_id text := 'u_' || replace(gen_random_uuid()::text, '-', '');
    v_credential_id text := 'cred_' || replace(gen_random_uuid()::text, '-', '');
    v_session_id text := 'sess_' || replace(gen_random_uuid()::text, '-', '');
    v_project_id text := 'proj_' || replace(gen_random_uuid()::text, '-', '');
    v_event_id text := 'aud_' || replace(gen_random_uuid()::text, '-', '');
    v_security_generation bigint := 1;
    v_constraint_name text;
BEGIN
    IF p_username IS NULL OR char_length(p_username) NOT BETWEEN 1 AND 16
       OR p_username_normalized IS NULL
       OR char_length(p_username_normalized) NOT BETWEEN 1 AND 16
       OR p_password_hash IS NULL
       OR p_password_hash NOT LIKE '$argon2id$%'
       OR p_hash_version IS NULL OR p_hash_version <= 0
       OR p_session_expires_at IS NULL
       OR p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'invalid account registration arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    INSERT INTO public.tenants (tenant_id, name)
    VALUES (v_tenant_id, p_username);

    INSERT INTO public.principals (principal_id, tenant_id, display_name)
    VALUES (v_principal_id, v_tenant_id, p_username);

    INSERT INTO public.projects (project_id, tenant_id, name)
    VALUES (v_project_id, v_tenant_id, '{DEFAULT_PROJECT_NAME}');

    INSERT INTO public.project_grants (tenant_id, principal_id, project_id)
    VALUES (v_tenant_id, v_principal_id, v_project_id);

    INSERT INTO public.account_credentials (
        credential_id, tenant_id, principal_id, username,
        username_normalized, password_hash, hash_version, security_generation
    ) VALUES (
        v_credential_id, v_tenant_id, v_principal_id, p_username,
        p_username_normalized COLLATE "C", p_password_hash, p_hash_version,
        v_security_generation
    );

    INSERT INTO public.user_sessions (
        session_id, tenant_id, principal_id, issued_at, expires_at,
        auth_method, credential_id, security_generation
    ) VALUES (
        v_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at,
        'password', v_credential_id, v_security_generation
    );

    PERFORM set_config('app.tenant_id', v_tenant_id, true);
    INSERT INTO public.auth_audit_outbox (
        event_id, event_type, payload, risk, tenant_id
    ) VALUES (
        v_event_id,
        'account_registered',
        jsonb_build_object(
            'principal_id', v_principal_id,
            'credential_id', v_credential_id,
            'default_project_id', v_project_id
        ),
        'high',
        v_tenant_id
    );

    RETURN QUERY SELECT
        v_credential_id,
        v_session_id,
        v_tenant_id,
        v_principal_id,
        p_session_expires_at,
        v_security_generation,
        v_project_id,
        '{DEFAULT_PROJECT_NAME}'::text;
EXCEPTION
    WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name = 'account_credentials_username_normalized_uq' THEN
            RAISE EXCEPTION 'username already registered'
                USING ERRCODE = 'unique_violation',
                      CONSTRAINT = 'account_credentials_username_normalized_uq';
        END IF;
        RAISE;
END;
$fn$;
"""


def _lookup_account_function() -> str:
    return """
CREATE OR REPLACE FUNCTION public.lookup_account_for_login(
    p_username_normalized text
)
RETURNS TABLE (
    credential_id text,
    tenant_id text,
    principal_id text,
    username text,
    password_hash text,
    hash_version integer,
    security_generation bigint,
    disabled_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog
AS $fn$
BEGIN
    IF p_username_normalized IS NULL
       OR char_length(p_username_normalized) NOT BETWEEN 1 AND 16
    THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        c.credential_id,
        c.tenant_id,
        c.principal_id,
        c.username,
        c.password_hash,
        c.hash_version,
        c.security_generation,
        c.disabled_at
      FROM public.account_credentials AS c
     WHERE c.username_normalized = p_username_normalized COLLATE "C";
END;
$fn$;
"""


def _complete_account_login_function() -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.complete_account_login(
    p_credential_id text,
    p_expected_security_generation bigint,
    p_session_expires_at timestamptz,
    p_new_password_hash text DEFAULT NULL,
    p_new_hash_version integer DEFAULT NULL
)
RETURNS TABLE (
    session_id text,
    tenant_id text,
    principal_id text,
    expires_at timestamptz,
    security_generation bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_session_id text := 'sess_' || replace(gen_random_uuid()::text, '-', '');
    v_event_id text := 'aud_' || replace(gen_random_uuid()::text, '-', '');
    v_tenant_id text;
    v_principal_id text;
    v_security_generation bigint;
BEGIN
    IF p_credential_id IS NULL OR char_length(p_credential_id) = 0
       OR p_expected_security_generation IS NULL
       OR p_expected_security_generation <= 0
       OR p_session_expires_at IS NULL
       OR p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
       OR ((p_new_password_hash IS NULL) <> (p_new_hash_version IS NULL))
       OR (p_new_password_hash IS NOT NULL AND p_new_password_hash NOT LIKE '$argon2id$%')
       OR (p_new_hash_version IS NOT NULL AND p_new_hash_version <= 0)
    THEN
        RAISE EXCEPTION 'invalid login completion arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT c.tenant_id, c.principal_id, c.security_generation
      INTO v_tenant_id, v_principal_id, v_security_generation
      FROM public.account_credentials AS c
     WHERE c.credential_id = p_credential_id
       AND c.security_generation = p_expected_security_generation
       AND c.disabled_at IS NULL
     FOR UPDATE;

    IF v_tenant_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.account_credentials AS c
       SET password_hash = COALESCE(p_new_password_hash, c.password_hash),
           hash_version = COALESCE(p_new_hash_version, c.hash_version),
           last_login_at = now(),
           updated_at = now()
     WHERE c.credential_id = p_credential_id;

    INSERT INTO public.user_sessions (
        session_id, tenant_id, principal_id, issued_at, expires_at,
        auth_method, credential_id, security_generation
    ) VALUES (
        v_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at,
        'password', p_credential_id, v_security_generation
    );

    PERFORM set_config('app.tenant_id', v_tenant_id, true);
    INSERT INTO public.auth_audit_outbox (
        event_id, event_type, payload, risk, tenant_id
    ) VALUES (
        v_event_id,
        'password_login_succeeded',
        jsonb_build_object(
            'principal_id', v_principal_id,
            'credential_id', p_credential_id
        ),
        'high',
        v_tenant_id
    );

    RETURN QUERY SELECT
        v_session_id,
        v_tenant_id,
        v_principal_id,
        p_session_expires_at,
        v_security_generation;
END;
$fn$;
"""


_RETRIEVAL_AGGREGATE = r"""retrieval_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object(
            'mode', retrieval_decision->>'mode',
            'reason_code', retrieval_decision->>'reason_code',
            'count', decision_count
        ) ORDER BY retrieval_decision->>'mode', retrieval_decision->>'reason_code'
    ) AS rows
    FROM (
        SELECT retrieval_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE retrieval_decision IS NOT NULL
        GROUP BY retrieval_decision
    ) AS grouped_retrievals
),
retrieval_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM retrieval_rows
)"""


def _metrics_function() -> str:
    """`study_metrics_snapshot()` —— **只保留 0019 版**（含检索模式聚合）。

    建表/加列完成后再创建：它引用 `routing_decision` / `retrieval_decision` /
    `provider_family` / `acquisition_fetch_observations` —— 任何一处未就位，
    函数在本基线里都会直接报错。
    """
    return (
        r"""
CREATE OR REPLACE FUNCTION public.study_metrics_snapshot()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH
job_totals AS (
    SELECT
        COALESCE(sum(attempt_count), 0)::bigint AS claims_total,
        count(*) FILTER (WHERE status = 'succeeded')::bigint AS succeeded,
        count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
        count(*) FILTER (WHERE status = 'unknown')::bigint AS unknown,
        count(*) FILTER (WHERE status = 'queued')::bigint AS queued,
        count(*) FILTER (WHERE status = 'running')::bigint AS running,
        count(*)::bigint AS all_jobs
    FROM public.acquisition_jobs
),
fetch_duration AS (
    SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
    FROM (
      SELECT jsonb_build_object(
            'outcome', outcome,
            'bucket_counts', jsonb_build_object(
                '0.1', count(*) FILTER (WHERE duration_seconds <= 0.1),
                '0.25', count(*) FILTER (WHERE duration_seconds <= 0.25),
                '0.5', count(*) FILTER (WHERE duration_seconds <= 0.5),
                '1', count(*) FILTER (WHERE duration_seconds <= 1),
                '2.5', count(*) FILTER (WHERE duration_seconds <= 2.5),
                '5', count(*) FILTER (WHERE duration_seconds <= 5),
                '10', count(*) FILTER (WHERE duration_seconds <= 10),
                '30', count(*) FILTER (WHERE duration_seconds <= 30),
                '+Inf', count(*)
            ),
            'count', count(*),
            'sum', COALESCE(sum(duration_seconds), 0)
      ) AS row_value
      FROM public.acquisition_fetch_observations
      WHERE duration_seconds IS NOT NULL
      GROUP BY outcome
    ) AS grouped_fetch_durations
),
fetch_bytes AS (
    SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
    FROM (
      SELECT jsonb_build_object(
            'outcome', outcome,
            'bucket_counts', jsonb_build_object(
                '1024', count(*) FILTER (WHERE response_body_bytes <= 1024),
                '4096', count(*) FILTER (WHERE response_body_bytes <= 4096),
                '16384', count(*) FILTER (WHERE response_body_bytes <= 16384),
                '65536', count(*) FILTER (WHERE response_body_bytes <= 65536),
                '262144', count(*) FILTER (WHERE response_body_bytes <= 262144),
                '1048576', count(*) FILTER (WHERE response_body_bytes <= 1048576),
                '+Inf', count(*)
            ),
            'count', count(*),
            'sum', COALESCE(sum(response_body_bytes), 0)
      ) AS row_value
      FROM public.acquisition_fetch_observations
      WHERE response_body_bytes IS NOT NULL
      GROUP BY outcome
    ) AS grouped_fetch_bytes
),
route_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object(
            'query_rewrite_status', routing_decision->>'query_rewrite_status',
            'reason_code', routing_decision->>'reason_code',
            'count', decision_count
        ) ORDER BY routing_decision->>'query_rewrite_status', routing_decision->>'reason_code'
    ) AS rows
    FROM (
        SELECT routing_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE routing_decision IS NOT NULL
        GROUP BY routing_decision
    ) AS grouped_routes
)"""
        + ",\n"
        + _RETRIEVAL_AGGREGATE
        + ",\n"
        + r"""
provider_base AS (
    SELECT
        p.provider_family AS provider,
        p.status AS attempt_status,
        p.input_tokens,
        p.output_tokens,
        CASE
            WHEN p.result_payload->>'provider_status' IN
                ('completed', 'refused', 'dispatch_failed', 'timeout', 'malformed', 'truncated')
                THEN p.result_payload->>'provider_status'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) = 'PROVIDER_DISPATCH_FAILED'
                THEN 'dispatch_failed'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_REFUSED', 'PROVIDER_REFUSED_USAGE_UNKNOWN') THEN 'refused'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_MALFORMED', 'PROVIDER_MALFORMED_USAGE_UNKNOWN') THEN 'malformed'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_TRUNCATED', 'PROVIDER_TRUNCATED_USAGE_UNKNOWN') THEN 'truncated'
            WHEN r.error_code = 'PROVIDER_TIMEOUT' THEN 'timeout'
            ELSE 'unknown'
        END AS outcome,
        CASE
            WHEN p.input_tokens IS NOT NULL AND p.output_tokens IS NOT NULL THEN 'reported'
            WHEN p.input_tokens IS NULL AND p.output_tokens IS NULL
                 AND p.status IN ('completed', 'failed') THEN 'missing'
            ELSE 'unknown'
        END AS usage_state
    FROM public.provider_attempts AS p
    JOIN public.teaching_runs AS r
      ON r.tenant_id = p.tenant_id
     AND r.project_id = p.project_id
     AND r.run_id = p.run_id
),
provider_attempt_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'outcome', outcome, 'count', attempt_count)
        ORDER BY provider, outcome
    ) AS rows
    FROM (
        SELECT provider, outcome, count(*)::bigint AS attempt_count
        FROM provider_base
        GROUP BY provider, outcome
    ) AS grouped_attempts
),
provider_token_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'direction', direction, 'count', token_count)
        ORDER BY provider, direction
    ) AS rows
    FROM (
        SELECT provider, 'input'::text AS direction, sum(input_tokens)::bigint AS token_count
        FROM provider_base
        WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL
        GROUP BY provider
        UNION ALL
        SELECT provider, 'output'::text AS direction, sum(output_tokens)::bigint AS token_count
        FROM provider_base
        WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL
        GROUP BY provider
    ) AS grouped_tokens
),
provider_usage_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'state', usage_state, 'count', usage_count)
        ORDER BY provider, usage_state
    ) AS rows
    FROM (
        SELECT provider, usage_state, count(*)::bigint AS usage_count
        FROM provider_base
        GROUP BY provider, usage_state
    ) AS grouped_usage
),
route_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM route_rows
),
attempt_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_attempt_rows
),
token_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_token_rows
),
usage_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_usage_rows
)
SELECT jsonb_build_object(
    'acquisition_claims_total', job_totals.claims_total,
    'acquisition_jobs_total', jsonb_build_object(
        'succeeded', job_totals.succeeded,
        'failed', job_totals.failed,
        'unknown', job_totals.unknown
    ),
    'acquisition_jobs', jsonb_build_object(
        'queued', job_totals.queued,
        'running', job_totals.running,
        'succeeded', job_totals.succeeded,
        'failed', job_totals.failed,
        'unknown', job_totals.unknown
    ),
    'fetch_duration', fetch_duration.rows,
    'response_body_bytes', fetch_bytes.rows,
    'route_decisions', route_json.rows,
    'retrieval_decisions', retrieval_json.rows,
    'provider_attempts', attempt_json.rows,
    'provider_tokens', token_json.rows,
    'provider_usage', usage_json.rows,
    'reconciliation_pending', jsonb_build_object(
        'acquisition', (
            SELECT count(*)::bigint FROM public.acquisition_jobs WHERE status = 'unknown'
        ),
        'teaching', (
            SELECT count(*)::bigint FROM public.teaching_runs WHERE status = 'reconciliation_required'
        )
    )
)
FROM job_totals CROSS JOIN fetch_duration CROSS JOIN fetch_bytes
     CROSS JOIN route_json CROSS JOIN attempt_json CROSS JOIN retrieval_json
     CROSS JOIN token_json CROSS JOIN usage_json
$function$
"""
    )


#: jsonb 数组元素必须为字符串的列（原 0020）。
_STRING_ARRAY_COLUMNS = (
    "acceptance_criteria",
    "evidence_required",
    "prerequisites",
)
_STRING_ARRAY_FUNCTION = "learning_jsonb_string_array"


def _string_array_function() -> str:
    return f"""
CREATE FUNCTION {_STRING_ARRAY_FUNCTION}(value jsonb) RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(value) AS item
           WHERE jsonb_typeof(item) <> 'string'
       )
$$
"""


def _secure_function(signature: str, *, executor_role: str) -> list[str]:
    """definer 函数的权限面：REVOKE PUBLIC/两角色，只 GRANT 给唯一的执行者。"""
    others = [role for role in (APP_ROLE, WORKER_ROLE) if role != executor_role]
    statements = [f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"]
    for role in others:
        statements.append(f"REVOKE ALL ON FUNCTION {signature} FROM {role}")
    statements.append(f"GRANT EXECUTE ON FUNCTION {signature} TO {executor_role}")
    return statements


# --------------------------------------------------------------------- 迁移


def upgrade() -> None:
    _assert_roles_exist()

    # ---- 核心表 + 项目级/主体级 RLS（原 0001、0002） --------------------
    for statement in _core_tables():
        op.execute(statement)
    # 核心表只有 confirmations / evidence_events / action_intents 是项目级
    # （原 0001 的 PROJECT_SCOPED）；`projects` 先在租户级下建策略，
    # 随后换成成员感知版。
    for table in (
        "tenants",
        "principals",
        "projects",
        "project_grants",
        "confirmations",
        "evidence_events",
        "action_intents",
    ):
        if table in ("confirmations", "evidence_events", "action_intents"):
            predicate = _tenant_project_predicate()
        else:
            predicate = _tenant_predicate()
        for statement in _rls_statements(table, predicate):
            op.execute(statement)

    for statement in _product_tables():
        op.execute(statement)
    for table in (
        "user_sessions",
        "conversations",
        "messages",
        "learning_plans",
        "milestones",
        "learning_tasks",
        "sources",
        "http_idempotency",
    ):
        if table in PRINCIPAL_SCOPED and table in PROJECT_SCOPED:
            predicate = _tenant_project_predicate(principal=True)
        elif table in PRINCIPAL_SCOPED:
            predicate = _tenant_principal_predicate()
        else:
            predicate = _tenant_project_predicate()
        for statement in _rls_statements(table, predicate):
            op.execute(statement)

    # projects 的策略换成成员感知版。
    for statement in _projects_membership_policy():
        op.execute(statement)

    # projects 的产品字段。
    op.execute("ALTER TABLE projects ADD COLUMN goal text NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE projects ADD COLUMN version integer NOT NULL DEFAULT 1 "
        "CHECK (version > 0)"
    )
    op.execute(
        "ALTER TABLE projects ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )

    for statement in _core_indexes():
        op.execute(statement)
    for statement in _product_indexes():
        op.execute(statement)

    # ---- 组合唯一键与组合外键（原 0003） --------------------------------
    for statement in _unique_parent_keys():
        op.execute(statement)
    for statement in _composite_foreign_keys():
        op.execute(statement)

    # ---- 会话期限硬上限（原 0004） -------------------------------------
    op.execute("ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_check")
    op.execute(
        "ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_ttl_check"
        " CHECK (expires_at > issued_at"
        f" AND expires_at <= issued_at + interval '{MAX_SESSION_TTL_DAYS} days')"
    )

    # ---- 认证限流的系统表与函数（原 0004/0006/0012） --------------------
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
    op.execute("ALTER TABLE auth_attempt_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_attempt_counters FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON auth_attempt_counters FROM PUBLIC")
    op.execute(f"REVOKE ALL ON auth_attempt_counters FROM {APP_ROLE}")
    op.execute(_rate_limit_function())
    for statement in _secure_function(
        "public.register_auth_attempt(text, integer)", executor_role=APP_ROLE
    ):
        op.execute(statement)

    # ---- 学习闭环（原 0005） -------------------------------------------
    for statement in _learning_loop_tables():
        op.execute(statement)
    for table in ("task_assessments", "task_submissions", "diagnoses"):
        if table in PRINCIPAL_SCOPED:
            predicate = _tenant_project_predicate(principal=True)
        else:
            predicate = _tenant_project_predicate()
        for statement in _rls_statements(table, predicate):
            op.execute(statement)
    for statement in _learning_loop_indexes():
        op.execute(statement)

    # ---- 认证审计 outbox 与幂等租约（原 0006） -------------------------
    op.execute("ALTER TABLE http_idempotency ADD COLUMN owner_token text")
    op.execute(
        "ALTER TABLE http_idempotency DROP CONSTRAINT IF EXISTS http_idempotency_state_check"
    )
    op.execute(
        "ALTER TABLE http_idempotency ADD CONSTRAINT http_idempotency_state_check"
        " CHECK (state IN ('pending', 'completed', 'released', 'indeterminate'))"
    )
    for statement in _audit_tables():
        op.execute(statement)
    op.execute("ALTER TABLE auth_audit_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_audit_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY auth_audit_outbox_isolation ON auth_audit_outbox"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    for statement in _audit_outbox_grants():
        op.execute(statement)
    op.execute(_audit_pending_count_function())
    for statement in _secure_function(
        "public.auth_audit_pending_count()", executor_role=APP_ROLE
    ):
        op.execute(statement)

    # ---- 资料摄取（原 0007/0008/0009） ---------------------------------
    for statement in _ingestion_tables():
        op.execute(statement)
    for table in ("source_documents", "ingestion_jobs", "source_chunks"):
        for statement in _rls_statements(table, _tenant_project_predicate()):
            op.execute(statement)
    for statement in _worker_policy("ingestion_jobs"):
        op.execute(statement)
    op.execute("ALTER TABLE ingestion_jobs ADD COLUMN claim_token uuid")
    op.execute(
        f"""
DO $$
DECLARE target record;
BEGIN
    FOR target IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = '{TABLE}'::regclass
           AND contype = 'c'
           AND (pg_get_constraintdef(oid) LIKE '%processing%'
                OR pg_get_constraintdef(oid) LIKE '%error_code%')
    LOOP
        EXECUTE format('ALTER TABLE {TABLE} DROP CONSTRAINT %I', target.conname);
    END LOOP;
END
$$
"""
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ingestion_jobs_lease_requires_processing"
        " CHECK ((status = 'processing') = (lease_owner IS NOT NULL"
        " AND lease_until IS NOT NULL AND claim_token IS NOT NULL))"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ingestion_jobs_failed_requires_error"
        " CHECK ((status = 'failed') = (error_code <> ''))"
    )
    for statement in _ingestion_indexes():
        op.execute(statement)

    # ---- 教学交互（原 0010） -------------------------------------------
    for statement in _teaching_tables():
        op.execute(statement)
    for table in (
        "teaching_runs",
        "provider_attempts",
        "teaching_events",
        "teaching_budgets",
        "teaching_reservations",
    ):
        if table == "teaching_runs":
            predicate = _tenant_project_predicate(principal=True)
        else:
            predicate = _tenant_project_predicate()
        for statement in _rls_statements(table, predicate):
            op.execute(statement)
    for statement in _rls_statements("teaching_tenant_budgets", _tenant_predicate()):
        op.execute(statement)
    for statement in _worker_policy("teaching_runs"):
        op.execute(statement)
    # provider_attempts 的不可变请求快照（原 0011）。
    op.execute(
        "ALTER TABLE provider_attempts "
        "ADD COLUMN request_payload jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    for statement in _teaching_indexes():
        op.execute(statement)

    # ---- 凭据、平台额度与认证函数（原 0012/0013） -----------------------
    for statement in _credential_tables():
        op.execute(statement)
    op.execute("INSERT INTO public.platform_budget_config (config_id) VALUES (true)")
    op.execute(
        "CREATE INDEX platform_paid_reservations_period_state_idx"
        " ON public.platform_paid_reservations (billing_period, state)"
    )
    op.execute("ALTER TABLE public.platform_budget_config ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.platform_budget_config FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.platform_paid_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.platform_paid_reservations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE UNIQUE INDEX account_credentials_username_normalized_uq"
        '    ON public.account_credentials (username_normalized COLLATE "C")'
    )
    op.execute("ALTER TABLE public.account_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.account_credentials FORCE ROW LEVEL SECURITY")
    op.execute(
        """
CREATE POLICY account_credentials_isolation ON public.account_credentials
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND principal_id = current_setting('app.principal_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND principal_id = current_setting('app.principal_id', true)
    )
"""
    )

    # 会话的凭据绑定列（`public.` 前缀保持原写法，见模块说明）。
    op.execute("ALTER TABLE public.user_sessions ADD COLUMN auth_method text")
    op.execute("ALTER TABLE public.user_sessions ADD COLUMN credential_id text")
    op.execute("ALTER TABLE public.user_sessions ADD COLUMN security_generation bigint")
    op.execute("ALTER TABLE public.user_sessions ALTER COLUMN auth_method SET NOT NULL")
    op.execute(
        "ALTER TABLE public.user_sessions ALTER COLUMN security_generation SET NOT NULL"
    )
    op.execute(
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_security_generation_check
    CHECK (security_generation > 0)
"""
    )
    # 只保留密码会话：邀请码会话分支随邀请码链路一并移除。
    op.execute(
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_auth_shape_check
    CHECK (
        auth_method = 'password' AND credential_id IS NOT NULL
    )
"""
    )
    op.execute(
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_credential_tenant_principal_fk
    FOREIGN KEY (tenant_id, principal_id, credential_id)
    REFERENCES account_credentials (tenant_id, principal_id, credential_id)
"""
    )
    op.execute(_invalidate_credential_sessions_function())
    op.execute(
        """
CREATE TRIGGER account_credentials_invalidate_sessions
AFTER UPDATE OF security_generation, disabled_at ON public.account_credentials
FOR EACH ROW EXECUTE FUNCTION public.invalidate_credential_sessions()
"""
    )
    # `invalidate_credential_sessions` 是触发器函数，**不**授任何 EXECUTE：
    # 原链只 REVOKE，应用与 worker 都不该手工调用它。
    for role in ("PUBLIC", APP_ROLE, WORKER_ROLE):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.invalidate_credential_sessions() FROM {role}"
        )

    # 平台额度函数（reserve 用 0013 形态），执行者只有 worker。
    op.execute(_reserve_platform_paid_function())
    for statement in _platform_transition_functions():
        op.execute(statement)
    # monthly_cap_micro 可为 NULL（不限月度金额，但仍记录与预留）。
    op.execute(
        "ALTER TABLE public.platform_budget_config "
        "ALTER COLUMN monthly_cap_micro DROP NOT NULL"
    )
    for signature in (
        "public.reserve_platform_paid(text, date, text, text, bigint)",
        "public.mark_platform_paid_in_flight(text)",
        "public.release_platform_paid_held(text)",
        "public.release_platform_paid_failed(text)",
        "public.settle_platform_paid(text, bigint)",
    ):
        for statement in _secure_function(signature, executor_role=WORKER_ROLE):
            op.execute(statement)

    # 注册 / 登录（执行者只有应用角色）。
    op.execute(_register_account_function())
    op.execute(_lookup_account_function())
    op.execute(_complete_account_login_function())
    for signature in (
        "public.register_account(text, text, text, integer, timestamptz)",
        "public.lookup_account_for_login(text)",
        "public.complete_account_login(text, bigint, timestamptz, text, integer)",
    ):
        for statement in _secure_function(signature, executor_role=APP_ROLE):
            op.execute(statement)

    # ---- 外部抓取候选与任务（原 0014/0015） ----------------------------
    for statement in _acquisition_tables():
        op.execute(statement)
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " DROP CONSTRAINT acquisition_jobs_media_type_check"
    )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " ADD CONSTRAINT acquisition_jobs_media_type_check"
        " CHECK (media_type IN ('text/plain', 'text/markdown'))"
    )
    for table in ("source_candidates", "acquisition_jobs"):
        for statement in _rls_statements(table, _tenant_project_predicate()):
            op.execute(statement)
    for statement in _worker_policy("acquisition_jobs"):
        op.execute(statement)
    for statement in _acquisition_indexes():
        op.execute(statement)

    # ---- 抓取字节与来源溯源（原 0016） ---------------------------------
    for statement in _artifact_tables():
        op.execute(statement)
    op.execute("ALTER TABLE source_fetch_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE source_fetch_artifacts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
CREATE POLICY source_fetch_artifacts_isolation ON source_fetch_artifacts
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
"""
    )
    op.execute(
        "ALTER TABLE source_documents ADD COLUMN fetch_attempt_id text"
    )
    op.execute(
        "ALTER TABLE source_documents ADD COLUMN source_content_type text"
    )
    op.execute(
        "ALTER TABLE source_documents ADD COLUMN raw_content_hash text"
    )
    op.execute(
        """
ALTER TABLE source_documents
    ADD CONSTRAINT source_documents_fetch_provenance_check
    CHECK (
        (
            acquisition_method = 'web_fetch'
            AND fetch_attempt_id IS NOT NULL
            AND source_content_type IN ('text/html', 'text/markdown', 'text/plain')
            AND raw_content_hash ~ '^sha256:[0-9a-f]{64}$'
            AND parser_version IN ('html-to-markdown/v1', 'web-text/v1')
        )
        OR
        (
            acquisition_method <> 'web_fetch'
            AND fetch_attempt_id IS NULL
            AND source_content_type IS NULL
            AND raw_content_hash IS NULL
        )
    )
"""
    )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " ADD CONSTRAINT acquisition_jobs_attempt_limit_check"
        " CHECK (attempt_count <= 3)"
    )
    op.execute(
        """
ALTER TABLE source_documents
    ADD CONSTRAINT source_documents_fetch_artifact_fk
    FOREIGN KEY (tenant_id, project_id, fetch_attempt_id)
    REFERENCES source_fetch_artifacts (tenant_id, project_id, acquisition_id)
"""
    )

    # ---- 路由决策（原 0017） -------------------------------------------
    op.execute("ALTER TABLE teaching_runs ADD COLUMN routing_decision jsonb")
    op.execute(
        """
ALTER TABLE teaching_runs
    ADD CONSTRAINT teaching_runs_routing_decision_check
    CHECK (
        routing_decision IS NULL
        OR (
            jsonb_typeof(routing_decision) = 'object'
            AND routing_decision ?& ARRAY[
                'policy_version', 'answer_route',
                'query_rewrite_status', 'reason_code'
            ]
            AND routing_decision - ARRAY[
                'policy_version', 'answer_route',
                'query_rewrite_status', 'reason_code'
            ] = '{}'::jsonb
            AND routing_decision->>'policy_version' = 'teaching-route/v1'
            AND routing_decision->>'answer_route' = 'cloud'
            AND (
                (routing_decision->>'query_rewrite_status' = 'disabled'
                 AND routing_decision->>'reason_code' = 'local_not_configured')
                OR
                (routing_decision->>'query_rewrite_status' = 'applied'
                 AND routing_decision->>'reason_code' = 'local_rewrite_accepted')
                OR
                (routing_decision->>'query_rewrite_status' = 'fallback'
                 AND routing_decision->>'reason_code' = 'local_unavailable_or_invalid')
            )
        )
    )
"""
    )

    # ---- 抓取指标观测（原 0018） ---------------------------------------
    op.execute(
        "ALTER TABLE public.provider_attempts"
        " ADD COLUMN provider_family text NOT NULL DEFAULT 'unknown'"
        " CHECK (provider_family IN ('openai', 'unknown'))"
    )
    for statement in _metrics_tables():
        op.execute(statement)
    op.execute("ALTER TABLE public.acquisition_fetch_observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.acquisition_fetch_observations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
CREATE POLICY acquisition_fetch_observations_scope ON public.acquisition_fetch_observations
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
"""
    )
    for statement in _metrics_indexes():
        op.execute(statement)

    # ---- 检索模式决策与指标函数（原 0019） ------------------------------
    op.execute("ALTER TABLE teaching_runs ADD COLUMN retrieval_decision jsonb")
    op.execute(
        """
ALTER TABLE teaching_runs
    ADD CONSTRAINT teaching_runs_retrieval_decision_check
    CHECK (
            retrieval_decision IS NULL
            OR (
                jsonb_typeof(retrieval_decision) = 'object'
                AND retrieval_decision ?& ARRAY[
                    'policy_version', 'mode', 'reason_code', 'ranking_version'
                ]
                AND retrieval_decision - ARRAY[
                    'policy_version', 'mode', 'reason_code', 'ranking_version'
                ] = '{}'::jsonb
                AND jsonb_typeof(retrieval_decision->'policy_version') = 'string'
                AND jsonb_typeof(retrieval_decision->'mode') = 'string'
                AND jsonb_typeof(retrieval_decision->'reason_code') = 'string'
                AND jsonb_typeof(retrieval_decision->'ranking_version') = 'string'
                AND retrieval_decision->>'policy_version' = 'retrieval-route/v1'
            AND retrieval_decision->>'mode' IN ('keyword', 'hybrid', 'degraded')
            AND (
                (
                    retrieval_decision->>'mode' <> 'degraded'
                    AND retrieval_decision->>'reason_code' = ''
                )
                OR
                (
                    retrieval_decision->>'mode' = 'degraded'
                    AND retrieval_decision->>'reason_code' IN (
                        'embedding_provider_failed',
                        'vector_index_unavailable',
                        'embedding_model_version_mismatch'
                    )
                )
            )
            AND retrieval_decision->>'ranking_version' <> ''
        )
    )
"""
    )
    # 快照函数：建在它引用的全部列/表之后。
    op.execute(_metrics_function())
    for statement in _secure_function(
        "public.study_metrics_snapshot()", executor_role=APP_ROLE
    ):
        op.execute(statement)

    # ---- 任务详情列（原 0020） -----------------------------------------
    for statement in (
        "ALTER TABLE learning_tasks ADD COLUMN objective text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN instruction text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN task_type text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN estimated_minutes integer NOT NULL DEFAULT 0",
        "ALTER TABLE learning_tasks ADD COLUMN deliverable text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN acceptance_criteria jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN evidence_required jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN prerequisites jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN related_skill_id text NOT NULL DEFAULT ''",
    ):
        op.execute(statement)
    op.execute(_string_array_function())
    op.execute(
        """
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_task_type_check
    CHECK (task_type IN ('', 'concept', 'practice', 'reflection'))
"""
    )
    op.execute(
        """
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_estimated_minutes_check
    CHECK (estimated_minutes BETWEEN 0 AND 600)
"""
    )
    for column in _STRING_ARRAY_COLUMNS:
        op.execute(
            f"""
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_{column}_check
    CHECK (
        jsonb_typeof({column}) = 'array'
        AND {_STRING_ARRAY_FUNCTION}({column})
    )
"""
        )

    # ---- 用户级共享知识库（原 0021） ------------------------------------
    for statement in _library_tables():
        op.execute(statement)
    for table in ("library_sources", "library_documents"):
        for statement in _rls_statements(table, _tenant_principal_predicate()):
            op.execute(statement)
    for statement in _library_indexes():
        op.execute(statement)

    # ---- 授权（应用角色、worker 角色与凭据边界） ------------------------
    for statement in _app_grants():
        op.execute(statement)
    for statement in _worker_grants():
        op.execute(statement)
    for statement in _credential_revocations():
        op.execute(statement)


#: downgrade 时删除的表（逆依赖顺序；CASCADE 处理策略、外键、索引与触发器）。
_DROP_TABLES = (
    "library_documents",
    "library_sources",
    "acquisition_fetch_observations",
    "source_fetch_artifacts",
    "acquisition_jobs",
    "source_candidates",
    "account_credentials",
    "platform_paid_reservations",
    "platform_budget_config",
    "teaching_reservations",
    "teaching_budgets",
    "teaching_tenant_budgets",
    "teaching_events",
    "provider_attempts",
    "teaching_runs",
    "source_chunks",
    "ingestion_jobs",
    "source_documents",
    "auth_audit_outbox",
    "diagnoses",
    "task_submissions",
    "task_assessments",
    "auth_attempt_counters",
    "http_idempotency",
    "sources",
    "learning_tasks",
    "milestones",
    "learning_plans",
    "messages",
    "conversations",
    "user_sessions",
    "action_intents",
    "evidence_events",
    "confirmations",
    "project_grants",
    "projects",
    "principals",
    "tenants",
)

#: downgrade 时删除的函数（独立对象，DROP TABLE 不会带走它们）。
_DROP_FUNCTIONS = (
    "public.study_metrics_snapshot()",
    "public.learning_jsonb_string_array(jsonb)",
    "public.complete_account_login(text, bigint, timestamptz, text, integer)",
    "public.lookup_account_for_login(text)",
    "public.register_account(text, text, text, integer, timestamptz)",
    "public.settle_platform_paid(text, bigint)",
    "public.release_platform_paid_failed(text)",
    "public.release_platform_paid_held(text)",
    "public.mark_platform_paid_in_flight(text)",
    "public.reserve_platform_paid(text, date, text, text, bigint)",
    "public.invalidate_credential_sessions()",
    "public.auth_audit_pending_count()",
    "public.register_auth_attempt(text, integer)",
)


def _downgrade_data_guard() -> str:
    """有业务数据时拒绝清库。

    `platform_budget_config` 的种子行由 upgrade 自己插入、upgrade 会重建，
    不算业务数据；其余任何 public 表还有行都视为"正在被使用"，直接失败。
    """
    return """
DO $$
DECLARE
    target record;
    v_rows bigint;
BEGIN
    FOR target IN
        SELECT c.relname FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relname <> 'alembic_version'
           AND c.relname <> 'platform_budget_config'
    LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', target.relname) INTO v_rows;
        IF v_rows > 0 THEN
            RAISE EXCEPTION
                'cannot downgrade the baseline while public.% still holds % row(s)',
                target.relname, v_rows
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
END
$$
"""


def downgrade() -> None:
    op.execute(_downgrade_data_guard())

    for table in _DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    for signature in _DROP_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute(f"REVOKE SELECT ON alembic_version FROM {APP_ROLE}")
