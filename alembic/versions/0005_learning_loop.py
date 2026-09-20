"""学习闭环 MVP：评估契约冻结、练习提交、基础诊断。

第五份迁移，为第 3 轮学习闭环补三张表。

## 设计要点

### 1. 评估契约必须「事前冻结」（task_assessments）

学习证据进入掌握投影的前提（01 号规格 §4）：必须引用事前冻结的
assessment contract 与有效 mapping。「事前」的物理形态就是这张表：
计划生成时写入 task → (component_id, contract_id, mapping_version) 的绑定，
此后**不可改**（应用角色只有 SELECT/INSERT，没有 UPDATE —— 想改 mapping
只能生成新计划、冻结新契约）。

「普通练习事后追认为 assessment」的门，从权限层面焊死。

### 2. 练习提交与诊断都是 append-only 事实

`task_submissions` / `diagnoses` 应用角色只有 SELECT/INSERT：
提交与自评是已经发生的事实，改写历史等于伪造证据链。

### 3. 组合外键（铁律 36 的延续）

三张表的 project/principal/task 引用全部用组合外键：
- `(tenant_id, project_id)` → `projects (tenant_id, project_id)`
- `(tenant_id, principal_id)` → `principals (tenant_id, principal_id)`
- `(tenant_id, project_id, task_id)` → `learning_tasks` 的同名组合唯一键（本迁移补建）

「租户 A 的提交挂在租户 B 的任务上」在物理上无法成立。

### 4. RLS 维度

- `task_assessments`：租户 + 项目（契约属于项目，项目成员都可见）；
- `task_submissions` / `diagnoses`：租户 + 项目 + **主体**（自己的提交与
  诊断自己可见 —— 严格优先，将来做协作学习再放宽）。

## 与 0001-0004 一致的约定

迁移自包含（不共享会演进的 helper）；角色不存在显式失败；downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: append-only 表：应用角色只有 SELECT / INSERT（改历史 = 伪造证据链）。
APPEND_ONLY = {"task_assessments", "task_submissions", "diagnoses"}

#: RLS 维度：这三张表都有 project_id 与 principal_id 列且 NOT NULL。
PROJECT_SCOPED = {"task_assessments", "task_submissions", "diagnoses"}
PRINCIPAL_SCOPED = {"task_submissions", "diagnoses"}


def _assert_app_role_exists() -> None:
    """角色不存在就显式失败 —— 与 0001-0004 一致。"""
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


def _create_tables() -> list[str]:
    return [
        # 0003 只给 principals / projects 建了组合唯一键；learning_tasks 的
        # 组合唯一键在这里补 —— 它是下面三列组合外键的父键。
        """
ALTER TABLE learning_tasks ADD CONSTRAINT learning_tasks_scope_key
    UNIQUE (tenant_id, project_id, task_id)
""",
        # ------------------------------------------------ 评估契约（事前冻结）
        # 每个任务至多一份契约（UNIQUE (task_id)）：重复生成计划用
        # ON CONFLICT DO NOTHING 幂等跳过，而不是覆盖 —— 覆盖等于解冻。
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
        # -------------------------------------------------------- 练习提交
        # mode 收窄为闭集：MVP 只有自报（self_report），第 4 轮接入模型评估
        # 时再扩枚举 —— 一个当前永不产生的模式比缺失的模式更糟。
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
        # -------------------------------------------------------- 基础诊断
        # 一次诊断一行；重新诊断追加新行（历史保留，摘要可对比）。
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


def _index_statements() -> list[str]:
    return [
        # 按任务取提交（判 verified 与回看历史）。
        "CREATE INDEX task_submissions_task_idx ON task_submissions (task_id, created_at)",
        # 按任务取评估契约（提交前的前置检查）。
        "CREATE INDEX task_assessments_task_idx ON task_assessments (task_id)",
    ]


def _rls_statements(table: str) -> list[str]:
    """租户 + 项目（+ 主体）谓词。与 0002 的 _standard_rls 同构，自包含副本。"""
    parts = ["tenant_id = current_setting('app.tenant_id', true)"]
    if table in PROJECT_SCOPED:
        parts.append("project_id = current_setting('app.project_id', true)")
    if table in PRINCIPAL_SCOPED:
        parts.append("principal_id = current_setting('app.principal_id', true)")
    predicate = "\n       AND ".join(parts)
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _grant_statements() -> list[str]:
    statements: list[str] = []
    for table in ("task_assessments", "task_submissions", "diagnoses"):
        # 本迁移三张表全部 append-only。
        statements.append(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    return statements


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _create_tables():
        op.execute(statement)

    for table in ("task_assessments", "task_submissions", "diagnoses"):
        for statement in _rls_statements(table):
            op.execute(statement)

    for statement in _index_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    # 与 upgrade 严格逆序，每一处都真的还原。
    op.execute("DROP INDEX IF EXISTS task_assessments_task_idx")
    op.execute("DROP INDEX IF EXISTS task_submissions_task_idx")
    op.execute("DROP TABLE IF EXISTS diagnoses")
    op.execute("DROP TABLE IF EXISTS task_submissions")
    op.execute("DROP TABLE IF EXISTS task_assessments")
    op.execute("ALTER TABLE learning_tasks DROP CONSTRAINT IF EXISTS learning_tasks_scope_key")
