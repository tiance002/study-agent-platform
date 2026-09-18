"""数据库连接配置。

DSN 一律从环境变量读，**不写进代码、不写进仓库**（项目红线：密钥不进仓库）。

两个角色、两个 DSN，绝不能混用：

| 用途 | 环境变量 | 角色 |
|---|---|---|
| 应用运行 | `STUDY_PLATFORM_DSN` | `study_app`（`NOSUPERUSER` + `NOBYPASSRLS`） |
| 迁移 | `STUDY_PLATFORM_MIGRATION_DSN` | 迁移角色（有 DDL 权限） |

**为什么应用角色绝不能有 DDL 权限**：拿到 DDL 就能
`ALTER TABLE ... DISABLE ROW LEVEL SECURITY` —— 那一刻，租户隔离从
「数据库强制」退化成「应用自觉」，前面所有 RLS 验证全部作废。
角色属性由 `scripts/sql/create_app_role.sql` 保证。

默认 URL **刻意不含密码**：本机开发库对本机连接使用 trust 认证，
所以能连上；这也意味着默认值本身不含任何秘密。
生产环境必须显式提供完整 DSN（含凭据），并由 KMS / Secret Manager 注入。
"""

from __future__ import annotations

import os

DEFAULT_APP_DSN = "postgresql://study_app@127.0.0.1:5432/study_platform"


def app_dsn() -> str:
    """应用连接串。生产必须通过环境变量显式提供。"""
    dsn = os.environ.get("STUDY_PLATFORM_DSN", DEFAULT_APP_DSN)
    if not dsn:
        raise RuntimeError(
            "STUDY_PLATFORM_DSN 为空。应用需要指向 study_app 角色的连接串，"
            "不能复用迁移角色。"
        )
    return dsn
