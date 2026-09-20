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

import pg_support
import psycopg
import pytest
from app.deployment import DeploymentSettings
from app.main import build_platform
from test_source_ingestion_repositories import _drain_queue

# ------------------------------------------------------------ 一、库名即凭证


def test_require_test_database_refuses_the_business_database():
    """业务库 DSN 必须被拒绝，且**在写入之前**失败。

    这是"误指业务 DSN 时 setup 写入前失败"的机械证明：
    `_drain_queue` / `create_test_database` 都先过这一道。
    """
    business = "postgresql://postgres@127.0.0.1:5432/study_platform"
    with pytest.raises(RuntimeError, match="study_test_"):
        pg_support.require_test_database(business)

    accepted = "postgresql://postgres@127.0.0.1:5432/study_test_abc123"
    assert pg_support.require_test_database(accepted) == accepted


def test_database_name_ignores_query_parameters_and_slash():
    assert pg_support.database_name("postgresql://u@h:5432/db?sslmode=disable") == "db"
    assert pg_support.database_name("postgresql://u@h:5432/") == ""
    assert pg_support.is_test_database("postgresql://u@h/study_test_x") is True
    assert pg_support.is_test_database(pg_support.business_dsn()) is False


# -------------------------------------------------- 二、临时库确实是临时库


@pytest.mark.postgres
def test_the_active_dsns_point_at_a_temporary_database(pg_database):
    """生效的三个 DSN 必须指向随机临时库，而不是业务库。

    "测试跑在别处"这件事必须是可断言的，而不是靠 review 读环境变量。
    """
    if pg_database is None:
        pytest.skip("本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")

    assert pg_database.name.startswith(pg_support.TEST_DATABASE_PREFIX)
    for dsn in (
        pg_support.migration_dsn(),
        pg_support.app_dsn(),
        pg_support.worker_dsn(),
    ):
        assert pg_support.is_test_database(dsn), dsn
        assert pg_support.database_name(dsn) == pg_database.name

    # 业务库与临时库是两个库 —— 否则下面那条哨兵断言等于自己跟自己比。
    assert pg_support.database_name(pg_support.business_dsn()) != pg_database.name


# ------------------------------------------------- 三、排空只打到临时库


@pytest.mark.postgres
@pytest.mark.invariant
def test_draining_the_queue_only_reaches_the_temporary_database(pg_database):
    """`_drain_queue()` 之后：临时库被排空，**业务库逐字段不变**。"""
    if pg_database is None:
        pytest.skip("本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")

    business = pg_support.business_dsn()
    business_ids = pg_support.seed_job(business, status="processing")
    temporary_ids = pg_support.seed_job(pg_database.migration_dsn)
    try:
        before = pg_support.job_state(business, business_ids.job_id)

        _drain_queue()

        # 阳性对照：排空**确实**跑过，并且打到了临时库里的那条。
        assert (
            pg_support.job_state(pg_database.migration_dsn, temporary_ids.job_id)["status"]
            == "failed"
        ), "临时库里的排队任务没有被排空 —— 那说明下面的'业务库没变'什么也没证明"

        assert pg_support.job_state(business, business_ids.job_id) == before, (
            "业务库里的 sentinel 被测试改动了：排空打错了库"
        )
    finally:
        pg_support.cleanup_job(business, business_ids)


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

    absent = pg_support.with_database(pg_support.app_dsn(), "study_test_absent_a1b2c3")
    monkeypatch.setenv("STUDY_PLATFORM_DSN", absent)

    settings = DeploymentSettings.load({"STUDY_PLATFORM_PERSISTENCE": "postgres"})
    assert settings.dsn is None, "前提：本用例测的正是'settings 未带 DSN'这条回退路径"

    with pytest.raises(psycopg.OperationalError) as failure:
        build_platform(var_dir=tmp_path, settings=settings)
    # 失败原因必须是"连不上那个不存在的库"，而不是别的启动问题。
    assert "study_test_absent_a1b2c3" in str(failure.value), failure.value
