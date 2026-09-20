"""数据库连接配置。

DSN 一律从环境变量读，**不写进代码、不写进仓库**（项目红线：密钥不进仓库）。

三个角色、三个 DSN，绝不能混用：

| 用途 | 环境变量 | 角色 |
|---|---|---|
| 应用运行 | `STUDY_PLATFORM_DSN` | `study_app`（`NOSUPERUSER` + `NOBYPASSRLS`） |
| 摄取 worker | `STUDY_PLATFORM_WORKER_DSN` | `study_worker`（同样的属性，但多一条队列策略） |
| 迁移 | `STUDY_PLATFORM_MIGRATION_DSN` | 迁移角色（有 DDL 权限） |

**为什么应用与 worker 必须是两个角色**：worker 要先发现"哪个租户有活干"，
才可能建立任何租户上下文 —— 所以它需要跨租户读队列表。这条能力若给应用角色，
任何连接都能 `set_config('app.worker_id', 'x')` 打开它（自定义 GUC 不是凭据），
于是"普通请求能读别人的队列"成为默认能力。0008 迁移把这条策略限到了
`TO study_worker`，本模块则保证凭据分开提供。

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

DEFAULT_WORKER_DSN = "postgresql://study_worker@127.0.0.1:5432/study_platform"


def app_dsn() -> str:
    """应用连接串。生产必须通过环境变量显式提供。"""
    dsn = os.environ.get("STUDY_PLATFORM_DSN", DEFAULT_APP_DSN)
    if not dsn:
        raise RuntimeError(
            "STUDY_PLATFORM_DSN 为空。应用需要指向 study_app 角色的连接串，"
            "不能复用迁移角色。"
        )
    return dsn


def worker_dsn() -> str:
    """摄取 worker 的连接串（`study_worker` 角色）。

    缺失时回退到本机 trust 默认值，与 `app_dsn()` 同样的取舍：默认值不含秘密，
    生产必须显式注入。**不回退到 `app_dsn()`** —— 那样队列的跨租户策略
    （0008 起限给 `study_worker`）会让 worker 一条任务也认领不到，
    而症状看起来像"队列空了"，不指向真正原因。
    """
    dsn = os.environ.get("STUDY_PLATFORM_WORKER_DSN", DEFAULT_WORKER_DSN)
    if not dsn:
        raise RuntimeError(
            "STUDY_PLATFORM_WORKER_DSN 为空。worker 需要指向 study_worker 角色的"
            "连接串：跨租户队列策略只授予该角色（见 0008 迁移）。"
        )
    return dsn
