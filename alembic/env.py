"""Alembic 运行环境。

两个刻意的设计，都和安全边界有关：

## 1. 连接串从环境变量读，不写进 `alembic.ini`

迁移需要 **DDL 权限**（建表、改策略），而应用角色 `study_app` 绝不能有 ——
否则一次误操作就能改表结构，RLS 也可能被绕过。所以两者用不同的 DSN：

| 用途 | 环境变量 | 角色 |
|---|---|---|
| 迁移 | `STUDY_PLATFORM_MIGRATION_DSN` | `postgres`（本地开发）/ 专门的迁移角色 |
| 应用 | `STUDY_PLATFORM_DSN` | `study_app`（`NOSUPERUSER` + `NOBYPASSRLS`） |

连接串里含密码，因此**不能进仓库**（项目红线）。

## 2. 不使用 autogenerate，迁移一律手写

本项目的表结构不只是"数据容器"，它还是**安全载体**：RLS 策略、列级 `GRANT`、
append-only 权限、`CHECK` 约束 —— 这些共同构成不变量 #2 的机械保障。

autogenerate 只能看见表和列，看不见策略与授权。用它生成迁移，会得到一堆
"看起来完整、实际把安全约束漏光"的 DDL。所以 `target_metadata` 显式为 `None`，
迁移内容一律手写 SQL，让每一处安全约束都在 review 里可见。
"""

from __future__ import annotations

import os

from alembic import context

# 本地开发默认值。注意它指向 `postgres` 超级用户 —— 仅限本机封闭环境，
# 且本机 pg_hba 对本机连接使用 trust。生产必须通过环境变量显式提供。
DEFAULT_MIGRATION_DSN = "postgresql+psycopg://postgres@127.0.0.1:5432/study_platform"

# 手写迁移，因此没有 metadata 可供比对。
target_metadata = None


def database_url() -> str:
    dsn = os.environ.get("STUDY_PLATFORM_MIGRATION_DSN", DEFAULT_MIGRATION_DSN)
    if not dsn:
        raise RuntimeError(
            "STUDY_PLATFORM_MIGRATION_DSN 为空。迁移需要具备 DDL 权限的连接串，"
            "不能复用应用角色。"
        )
    return dsn


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`：只产出 SQL，不连库。"""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(database_url(), future=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
