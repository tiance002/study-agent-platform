"""任务的"详情"列：目标、指令、类型、时长、交付物、验收与前置。

第二十份迁移。A02：让 `learning_tasks` 能承载真实分解的产物，
而不是只有一行标题。全部列带默认值 —— 既有手工计划行照旧有效，
只有生成计划会填满它们（质量不变量由 `backend/tests/test_plan_quality.py` 锁定）。

## 为什么只加列、不加 RLS

`learning_tasks` 在 0002 已经建立了租户 + 项目 RLS 与应用角色权限。
本迁移不新建表，只是给同一张表补列：`ALTER TABLE` 不改写策略，
所以**不需要重复声明 RLS 或 GRANT**（重复反而制造两份可漂移的真相）。

## 为什么 jsonb 元素"必须是字符串"要用函数表达

PostgreSQL 的 `CHECK` **不允许子查询**，而判断"jsonb 数组的每个元素都是
字符串"天然需要展开数组（`jsonb_array_elements`）。所以这里定义一个
`IMMUTABLE` 的 SQL 函数把该判断封装起来，`CHECK` 直接调用它 ——
规则仍由数据库兜底，而不是只靠应用层自觉。

## 与 0001-0019 一致的约定

迁移自包含、角色不存在显式失败、downgrade 真还原（删列、删约束、删函数）。
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 元素必须为字符串的 jsonb 数组列。
_STRING_ARRAY_COLUMNS = (
    "acceptance_criteria",
    "evidence_required",
    "prerequisites",
)

_STRING_ARRAY_FUNCTION = "learning_jsonb_string_array"


def _assert_app_role_exists() -> None:
    """角色不存在就显式失败 —— 与 0001-0019 一致。"""
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


def _add_column_statements() -> list[str]:
    # 一条 ADD COLUMN 一个语句字面量：契约生成器靠正则读
    # `ALTER TABLE ... ADD COLUMN`，把列名嵌进 f-string 会让它一条也读不到。
    return [
        "ALTER TABLE learning_tasks ADD COLUMN objective text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN instruction text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN task_type text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN estimated_minutes integer NOT NULL DEFAULT 0",
        "ALTER TABLE learning_tasks ADD COLUMN deliverable text NOT NULL DEFAULT ''",
        "ALTER TABLE learning_tasks ADD COLUMN acceptance_criteria jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN evidence_required jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN prerequisites jsonb NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE learning_tasks ADD COLUMN related_skill_id text NOT NULL DEFAULT ''",
    ]


def _constraint_statements() -> list[str]:
    statements = [
        f"""
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
""",
        # task_type 闭集（空串属于手工计划；生成计划取 concept/practice/reflection）。
        """
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_task_type_check
    CHECK (task_type IN ('', 'concept', 'practice', 'reflection'))
""",
        # 时长：0（手工计划未填）到 600 分钟。
        """
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_estimated_minutes_check
    CHECK (estimated_minutes BETWEEN 0 AND 600)
""",
    ]
    for column in _STRING_ARRAY_COLUMNS:
        statements.append(
            f"""
ALTER TABLE learning_tasks
    ADD CONSTRAINT learning_tasks_{column}_check
    CHECK (
        jsonb_typeof({column}) = 'array'
        AND {_STRING_ARRAY_FUNCTION}({column})
    )
"""
        )
    return statements


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _add_column_statements():
        op.execute(statement)

    for statement in _constraint_statements():
        op.execute(statement)


def downgrade() -> None:
    # 与 upgrade 严格逆序，每一处都真的还原。
    for column in _STRING_ARRAY_COLUMNS:
        op.execute(
            f"ALTER TABLE learning_tasks DROP CONSTRAINT IF EXISTS learning_tasks_{column}_check"
        )
    op.execute(
        "ALTER TABLE learning_tasks DROP CONSTRAINT IF EXISTS learning_tasks_estimated_minutes_check"
    )
    op.execute(
        "ALTER TABLE learning_tasks DROP CONSTRAINT IF EXISTS learning_tasks_task_type_check"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {_STRING_ARRAY_FUNCTION}(jsonb)")
    for column in (
        "related_skill_id",
        "prerequisites",
        "evidence_required",
        "acceptance_criteria",
        "deliverable",
        "estimated_minutes",
        "task_type",
        "instruction",
        "objective",
    ):
        op.execute(f"ALTER TABLE learning_tasks DROP COLUMN IF EXISTS {column}")
