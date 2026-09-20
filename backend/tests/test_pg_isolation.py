"""测试库隔离的守卫：**跑测试不得改变业务库里的任何一行**。

第 4 轮的 `_drain_queue()` 默认连业务库，把全库 `queued` / `processing` 任务
写成 `failed`。这两条断言锁住修复后的行为：

1. **库名即凭证**：`require_test_database()` 对业务库 DSN 必须拒绝
   （纯函数断言，不需要数据库）；
2. **排空只打到临时库**：同一个 `_drain_queue()` 调用之后，临时库里的
   sentinel 被排空、业务库里的 sentinel **逐字段不变**。

第 2 条内建**阳性对照**：临时库那条必须是 `TEST_DRAIN`。否则"业务库没变"
在"排空压根没跑"时也会通过 —— 那时它测的是"什么都没发生"，不是"打对了地方"。

业务库里的 sentinel 是 `processing` + **有效租约**：既落在排空的靶子里
（`status IN ('queued','processing')`），又不会被真实运行的 worker 抢走
（租约未过期不可认领）—— 用一条排队中的任务会把"有人刚好在跑 worker"
变成偶发失败。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pgtest
import psycopg
import pytest
from app.core.hashing import content_hash
from app.deployment import DeploymentSettings
from app.main import build_platform
from test_source_ingestion_repositories import _drain_queue

CONTENT = "# sentinel\n\n哨兵段落。\n"

#: sentinel 的租约时长。足够长：本条用例不可能跑一小时，
#: 而有效租约让真实运行的 worker 无法认领它（见模块 docstring）。
_ONE_HOUR = timedelta(hours=1)


# ------------------------------------------------------------ 一、库名即凭证


def test_require_test_database_refuses_the_business_database():
    """业务库 DSN 必须被拒绝，且**在写入之前**失败。

    这是"误指业务 DSN 时 setup 写入前失败"的机械证明：
    `_drain_queue` / `create_test_database` 都先过这一道。
    """
    business = "postgresql://postgres@127.0.0.1:5432/study_platform"
    with pytest.raises(RuntimeError, match="study_test_"):
        pgtest.require_test_database(business)

    accepted = "postgresql://postgres@127.0.0.1:5432/study_test_abc123"
    assert pgtest.require_test_database(accepted) == accepted


def test_database_name_ignores_query_parameters_and_slash():
    assert pgtest.database_name("postgresql://u@h:5432/db?sslmode=disable") == "db"
    assert pgtest.database_name("postgresql://u@h:5432/") == ""
    assert pgtest.is_test_database("postgresql://u@h/study_test_x") is True
    assert pgtest.is_test_database(pgtest.business_dsn()) is False


# -------------------------------------------------- 二、临时库确实是临时库


@pytest.mark.postgres
def test_the_active_dsns_point_at_a_temporary_database(pg_database):
    """生效的三个 DSN 必须指向随机临时库，而不是业务库。

    "测试跑在别处"这件事必须是可断言的，而不是靠 review 读环境变量。
    """
    if pg_database is None:
        pytest.skip("本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")

    assert pg_database.name.startswith(pgtest.TEST_DATABASE_PREFIX)
    for dsn in (
        pgtest.migration_dsn(),
        pgtest.app_dsn(),
        pgtest.worker_dsn(),
    ):
        assert pgtest.is_test_database(dsn), dsn
        assert pgtest.database_name(dsn) == pg_database.name

    # 业务库与临时库是两个库 —— 否则下面那条哨兵断言等于自己跟自己比。
    assert pgtest.database_name(pgtest.business_dsn()) != pg_database.name


# ------------------------------------------------- 三、排空只打到临时库


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_job(dsn: str, *, queued: bool) -> dict[str, str]:
    """在 `dsn` 指向的库里造一条 sentinel 任务（含完整的父链）。

    `queued=True` 造一条排队中的任务（排空的靶子）；
    `queued=False` 造一条 `processing` 且**租约有效**的任务（同样是靶子，
    但不会被真实 worker 认领）。
    """
    tag = uuid.uuid4().hex[:10]
    ids = {
        "tenant_id": f"t_sentinel_{tag}",
        "principal_id": f"u_sentinel_{tag}",
        "project_id": f"proj_sentinel_{tag}",
        "source_id": f"src_sentinel_{tag}",
        "document_id": f"doc_sentinel_{tag}",
        "job_id": f"job_sentinel_{tag}",
    }
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
            (ids["tenant_id"], "sentinel"),
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
            (ids["principal_id"], ids["tenant_id"]),
        )
        conn.execute(
            "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)",
            (ids["project_id"], ids["tenant_id"], "sentinel"),
        )
        conn.execute(
            "INSERT INTO sources (source_id, tenant_id, project_id, display_name,"
            " media_type, identity_hash, acquisition)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)",
            (
                ids["source_id"],
                ids["tenant_id"],
                ids["project_id"],
                "sentinel.md",
                "text/markdown",
                "sha256:" + tag,
            ),
        )
        conn.execute(
            "INSERT INTO source_documents (document_id, tenant_id, project_id,"
            " source_id, version, document_title, content, content_hash, media_type,"
            " language, parser_version, acquisition_method, observed_at)"
            " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 'text/markdown', 'zh',"
            " 'text/v1', 'upload', %s)",
            (
                ids["document_id"],
                ids["tenant_id"],
                ids["project_id"],
                ids["source_id"],
                "sentinel",
                CONTENT,
                content_hash(CONTENT),
                _now(),
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_jobs (job_id, tenant_id, project_id, source_id,"
            " document_id, status, attempt_count, lease_owner, lease_until)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ids["job_id"],
                ids["tenant_id"],
                ids["project_id"],
                ids["source_id"],
                ids["document_id"],
                "queued" if queued else "processing",
                0 if queued else 1,
                None if queued else "sentinel-holder",
                None if queued else _now().replace(microsecond=0) + _ONE_HOUR,
            ),
        )
    return ids


def _job_state(dsn: str, job_id: str) -> dict:
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


def _cleanup_job(dsn: str, ids: dict[str, str]) -> None:
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("DELETE FROM ingestion_jobs WHERE job_id = %s", (ids["job_id"],))
        conn.execute(
            "DELETE FROM source_documents WHERE document_id = %s", (ids["document_id"],)
        )
        conn.execute("DELETE FROM sources WHERE source_id = %s", (ids["source_id"],))
        conn.execute("DELETE FROM projects WHERE project_id = %s", (ids["project_id"],))
        conn.execute(
            "DELETE FROM principals WHERE principal_id = %s", (ids["principal_id"],)
        )
        conn.execute("DELETE FROM tenants WHERE tenant_id = %s", (ids["tenant_id"],))


@pytest.mark.postgres
@pytest.mark.invariant
def test_draining_the_queue_only_reaches_the_temporary_database(pg_database):
    """`_drain_queue()` 之后：临时库被排空，**业务库逐字段不变**。"""
    if pg_database is None:
        pytest.skip("本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")

    business = pgtest.business_dsn()
    business_ids = _seed_job(business, queued=False)
    temporary_ids = _seed_job(pg_database.migration_dsn, queued=True)
    try:
        before = _job_state(business, business_ids["job_id"])

        _drain_queue()

        # 阳性对照：排空**确实**跑过，并且打到了临时库里的那条。
        assert (
            _job_state(pg_database.migration_dsn, temporary_ids["job_id"])["status"]
            == "failed"
        ), "临时库里的排队任务没有被排空 —— 那说明下面的'业务库没变'什么也没证明"

        assert _job_state(business, business_ids["job_id"]) == before, (
            "业务库里的 sentinel 被测试改动了：排空打错了库"
        )
    finally:
        _cleanup_job(business, business_ids)


# ------------------------------- 四、装配层必须尊重环境里的 DSN


@pytest.mark.postgres
def test_platform_honours_the_environment_dsn(pg_database, monkeypatch, tmp_path):
    """`build_platform` 必须使用**环境变量里的** DSN，而不是硬编码的业务库默认值。

    实测缺陷（本轮修复）：组合根写的是 `dsn = loaded.dsn or DEFAULT_APP_DSN`，
    于是"`STUDY_PLATFORM_DSN` 说临时库、装配却连业务库"成为可能 ——
    而两边看起来都正常。测试库隔离正是从这条路被绕开的。

    证伪方式：把环境里的 DSN 指向一个**不存在的库**。装配层若真读环境变量，
    启动自检必然连不上而失败；若它回退到硬编码默认值，业务库连得上，
    这个用例就会红。
    """
    if pg_database is None:
        pytest.skip("本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")

    absent = pgtest.with_database(pgtest.app_dsn(), "study_test_absent_a1b2c3")
    monkeypatch.setenv("STUDY_PLATFORM_DSN", absent)

    settings = DeploymentSettings.load({"STUDY_PLATFORM_PERSISTENCE": "postgres"})
    assert settings.dsn is None, "前提：本用例测的正是'settings 未带 DSN'这条回退路径"

    with pytest.raises(psycopg.OperationalError) as failure:
        build_platform(var_dir=tmp_path, settings=settings)
    # 失败原因必须是"连不上那个不存在的库"，而不是别的启动问题。
    assert "study_test_absent_a1b2c3" in str(failure.value), failure.value
