"""PostgreSQL 测试必须跑在**随机临时库**上，绝不碰业务库。

## 为什么这是执行前置条件，而不是"测试卫生"

第 4 轮的 `_drain_queue()` 为了让"队列里只剩本用例那一条"，用超级用户
把所有 `queued` / `processing` 任务写成 `failed`。它的 DSN 默认指向
**业务库 `study_platform`** —— 于是跑一次测试就终结用户全部在途摄取任务。

这条破坏是**完全不可见**的：测试全绿，用户看到的是"上传成功但永远搜不到"。
所以隔离不是"让测试更干净"，而是"让测试不成为故障源"。

## 两条约束

1. **库名即凭证**：破坏性写入前必须 `require_test_database()`，
   库名不以 `study_test_` 开头一律拒绝。误指业务库时在**写入之前**失败。
2. **每个测试会话一个新库**：随机后缀，会话结束 `DROP ... WITH (FORCE)`。
   并行跑两个会话时，两边的排空互不影响 —— 而不是互相清掉对方的用例数据。

## 环境变量是"当前生效的 DSN"的唯一出口

夹具在会话开始时把 `STUDY_PLATFORM_*_DSN` 指向临时库，测试代码一律通过
`migration_dsn()` / `app_dsn()` / `worker_dsn()` **在调用时**读取 ——
不要在模块层赋值成常量：那样读到的是**会话夹具运行之前**的值（即业务库），
而它看起来完全正常，只是整个套件的写入都跑错了地方。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from app.core.hashing import content_hash

#: 测试库的库名前缀。**库名即凭证**：只有这个前缀的库允许被排空/重建。
TEST_DATABASE_PREFIX = "study_test_"

#: 仓库根目录（`backend/tests/pg_support.py` 往上三层）。
ROOT = Path(__file__).resolve().parents[2]

#: `CREATE` / `DROP DATABASE` 要连到 `postgres` 维护库，不能连目标库本身。
MAINTENANCE_DATABASE = "postgres"

#: 迁移角色（超级用户）的默认 DSN —— 业务库。
DEFAULT_ADMIN_DSN = "postgresql://postgres@127.0.0.1:5432/study_platform"
#: 应用角色与 worker 角色的默认 DSN。本机 trust 认证，所以都不含密码。
DEFAULT_APP_DSN = "postgresql://study_app@127.0.0.1:5432/study_platform"
DEFAULT_WORKER_DSN = "postgresql://study_worker@127.0.0.1:5432/study_platform"

#: ⚠️ 在**导入时**记下"业务库"是谁 —— 会话夹具随后会把环境变量改指向临时库，
#: 之后再读就分不清哪个是业务库了。哨兵用例正是要断言"业务库没被碰过"。
_BASE_ADMIN_DSN = os.environ.get("STUDY_PLATFORM_MIGRATION_DSN") or DEFAULT_ADMIN_DSN
_BASE_APP_DSN = os.environ.get("STUDY_PLATFORM_DSN") or DEFAULT_APP_DSN
_BASE_WORKER_DSN = os.environ.get("STUDY_PLATFORM_WORKER_DSN") or DEFAULT_WORKER_DSN

#: 夹具会接管这三个环境变量（也是"当前生效 DSN"的全部来源）。
DSN_ENV_VARS = (
    "STUDY_PLATFORM_MIGRATION_DSN",
    "STUDY_PLATFORM_DSN",
    "STUDY_PLATFORM_WORKER_DSN",
)


# ------------------------------------------------------------------ DSN 工具


def database_name(dsn: str) -> str:
    """从 DSN 里取库名（不含前导斜杠与查询串）。"""
    return urlsplit(dsn).path.lstrip("/")


def is_test_database(dsn: str) -> bool:
    return database_name(dsn).startswith(TEST_DATABASE_PREFIX)


def require_test_database(dsn: str) -> str:
    """破坏性操作前的唯一闸门。**在写入之前**失败，而不是写到一半才发现。"""
    name = database_name(dsn)
    if not is_test_database(dsn):
        raise RuntimeError(
            f"拒绝在非测试库上执行破坏性操作：库名为 {name!r}，"
            f"必须以 {TEST_DATABASE_PREFIX} 开头。"
            "（业务库里的在途任务是一条条真实的用户资料，测试无权终结它们）"
        )
    return dsn


def with_database(dsn: str, name: str) -> str:
    """换掉 DSN 里的库名，其余部分（角色、主机、参数）保持不变。"""
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path="/" + name))


def business_dsn() -> str:
    """业务库的迁移角色 DSN。**只用于只读断言**（哨兵用例）。"""
    return _BASE_ADMIN_DSN


def maintenance_dsn(base: str | None = None) -> str:
    return with_database(base or _BASE_ADMIN_DSN, MAINTENANCE_DATABASE)


def migration_dsn() -> str:
    """当前生效的**测试库**迁移 DSN（超级用户）。"""
    return os.environ.get("STUDY_PLATFORM_MIGRATION_DSN") or _BASE_ADMIN_DSN


def app_dsn() -> str:
    """当前生效的应用角色 DSN。"""
    return os.environ.get("STUDY_PLATFORM_DSN") or _BASE_APP_DSN


def worker_dsn() -> str:
    """当前生效的 worker 角色 DSN。

    ⚠️ 与 `app_dsn()` 是两个**凭据边界**，不能互相顶替：队列的跨租户可见性
    只授予 worker 角色（0008 迁移的 `TO study_worker`）。
    """
    return os.environ.get("STUDY_PLATFORM_WORKER_DSN") or _BASE_WORKER_DSN


def reachable(*, dsn: str | None = None, timeout: int = 2) -> bool:
    try:
        with psycopg.connect(dsn or _BASE_ADMIN_DSN, connect_timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — 探活要吞掉一切连接错误
        return False


# ------------------------------------------------------------------ 临时库


@dataclass(frozen=True)
class TestDatabase:
    """一个已经迁移到 head 的随机临时库。"""

    name: str
    migration_dsn: str
    app_dsn: str
    worker_dsn: str

    def env(self) -> dict[str, str]:
        """会被写进环境变量的四个值（含迁移 DSN，指向同一个测试库）。"""
        return {
            "STUDY_PLATFORM_MIGRATION_DSN": self.migration_dsn,
            "STUDY_PLATFORM_DSN": self.app_dsn,
            "STUDY_PLATFORM_WORKER_DSN": self.worker_dsn,
        }


def _admin(dsn: str, sql: str, params: tuple = ()) -> None:
    """在维护库上执行一条 DDL（自动提交 —— `CREATE DATABASE` 不能在事务里）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql, params)


def create_test_database() -> TestDatabase:
    """建一个随机名的测试库并迁移到 head。

    先 `require_test_database` 再 `CREATE`：名字是我们自己拼的，但这条断言
    把"库名即凭证"的规则钉在**唯一**一处，而不是靠每处调用点自己记得。
    """
    name = TEST_DATABASE_PREFIX + uuid.uuid4().hex[:12]
    database = TestDatabase(
        name=name,
        migration_dsn=with_database(_BASE_ADMIN_DSN, name),
        app_dsn=with_database(_BASE_APP_DSN, name),
        worker_dsn=with_database(_BASE_WORKER_DSN, name),
    )
    require_test_database(database.migration_dsn)

    admin = maintenance_dsn()
    _admin(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    _admin(admin, f'CREATE DATABASE "{name}"')
    _migrate(database.migration_dsn)
    return database


def _migrate(dsn: str) -> None:
    """在临时库上跑 `alembic upgrade head`。

    迁移是表结构（含 RLS 策略、列级 GRANT、组合外键）的唯一来源 ——
    测试库必须与业务库**由同一份迁移**造出来，否则测的是另一套结构。
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "STUDY_PLATFORM_MIGRATION_DSN": dsn},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "测试库迁移失败（临时库是新建的，失败通常意味着迁移本身有问题）：\n"
            + (result.stdout or "")[-2000:]
            + (result.stderr or "")[-2000:]
        )


def drop_test_database(database: TestDatabase) -> None:
    """删除临时库。**先校验库名** —— 这个函数里的 DROP 是不可逆的。"""
    require_test_database(database.migration_dsn)
    _admin(
        maintenance_dsn(),
        f'DROP DATABASE IF EXISTS "{database.name}" WITH (FORCE)',
    )


@contextmanager
def temp_test_database() -> Iterator[TestDatabase]:
    """用完即删的临时库（供独立脚本使用；测试套件走会话夹具）。"""
    database = create_test_database()
    try:
        yield database
    finally:
        drop_test_database(database)


# ------------------------------------------------------------- 队列哨兵与状态

#: 哨兵任务的原文。内容不重要，重要的是它经过完整的父链（租户→项目→资料→原文→任务），
#: 与真实摄取写入的行**结构完全相同** —— 否则它证明不了真实写入路径的行为。
SENTINEL_CONTENT = "# sentinel\n\n哨兵段落。\n"

#: 哨兵租约时长。足够长：用例不可能跑一小时；而**有效租约**让真实运行的 worker
#: 无法认领它 —— 用一条排队中的任务会把"有人刚好在跑 worker"变成偶发失败。
SENTINEL_LEASE = timedelta(hours=1)


@dataclass(frozen=True)
class SeededJob:
    tenant_id: str
    principal_id: str
    project_id: str
    source_id: str
    document_id: str
    job_id: str


def seed_job(dsn: str, *, status: str = "queued", tag: str | None = None) -> SeededJob:
    """在 `dsn` 指向的库里造一条 sentinel 任务（含完整父链）。

    `status="queued"` 造排队中的任务（排空的靶子）；
    `status="processing"` 造带**有效租约**的处理中任务（同样是排空的靶子，
    但不会被真实 worker 认领）。
    """
    suffix = tag or uuid.uuid4().hex[:10]
    ids = SeededJob(
        tenant_id=f"t_sentinel_{suffix}",
        principal_id=f"u_sentinel_{suffix}",
        project_id=f"proj_sentinel_{suffix}",
        source_id=f"src_sentinel_{suffix}",
        document_id=f"doc_sentinel_{suffix}",
        job_id=f"job_sentinel_{suffix}",
    )
    processing = status == "processing"
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
            (ids.tenant_id, "sentinel"),
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
            (ids.principal_id, ids.tenant_id),
        )
        conn.execute(
            "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)",
            (ids.project_id, ids.tenant_id, "sentinel"),
        )
        conn.execute(
            "INSERT INTO sources (source_id, tenant_id, project_id, display_name,"
            " media_type, identity_hash, acquisition)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)",
            (
                ids.source_id,
                ids.tenant_id,
                ids.project_id,
                "sentinel.md",
                "text/markdown",
                "sha256:" + suffix,
            ),
        )
        conn.execute(
            "INSERT INTO source_documents (document_id, tenant_id, project_id,"
            " source_id, version, document_title, content, content_hash, media_type,"
            " language, parser_version, acquisition_method, observed_at)"
            " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 'text/markdown', 'zh',"
            " 'text/v1', 'upload', %s)",
            (
                ids.document_id,
                ids.tenant_id,
                ids.project_id,
                ids.source_id,
                "sentinel",
                SENTINEL_CONTENT,
                content_hash(SENTINEL_CONTENT),
                datetime.now(timezone.utc),
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_jobs (job_id, tenant_id, project_id, source_id,"
            " document_id, status, attempt_count, lease_owner, lease_until)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ids.job_id,
                ids.tenant_id,
                ids.project_id,
                ids.source_id,
                ids.document_id,
                status,
                1 if processing else 0,
                "sentinel-holder" if processing else None,
                datetime.now(timezone.utc) + SENTINEL_LEASE if processing else None,
            ),
        )
    return ids


def job_state(dsn: str, job_id: str) -> dict:
    """任务的可观测状态。**逐字段比较**，不只看 status。"""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT status, attempt_count, error_code, error_detail, lease_owner,"
            " updated_at FROM ingestion_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert row is not None, f"{job_id} 不见了 —— 那本身就是被改动过的证据"
    return {
        "status": row[0],
        "attempt_count": row[1],
        "error_code": row[2],
        "error_detail": row[3],
        "lease_owner": row[4],
        "updated_at": row[5],
    }


def cleanup_job(dsn: str, ids: SeededJob) -> None:
    """把哨兵连同它的父链删干净（只删自己造的行）。"""
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("DELETE FROM ingestion_jobs WHERE job_id = %s", (ids.job_id,))
        conn.execute(
            "DELETE FROM source_documents WHERE document_id = %s", (ids.document_id,)
        )
        conn.execute("DELETE FROM sources WHERE source_id = %s", (ids.source_id,))
        conn.execute("DELETE FROM projects WHERE project_id = %s", (ids.project_id,))
        conn.execute(
            "DELETE FROM principals WHERE principal_id = %s", (ids.principal_id,)
        )
        conn.execute("DELETE FROM tenants WHERE tenant_id = %s", (ids.tenant_id,))
