"""新数据库基线（`0001`）的初始化测试。

原 `test_open_registration_migration.py` 依赖 `0011 → 0012` 的往返与
邀请码 provenance 回填；把 21 条迁移压成一条干净基线后，那些用例
所验证的对象已不存在，因此整体替换为这一份**基线初始化测试**。

它回答四个问题（全部在随机 `study_test_*` 临时库上）：

1. **空库一次 `alembic upgrade head` 成功** —— 单条迁移建成完整结构；
2. **关键表 / 函数 / 策略存在** —— 且 worker 策略按角色（`study_worker`）绑定；
3. **RLS 真的生效** —— 缺上下文 0 行、错主体 0 行、正确上下文可见；
4. **没有任何邀请码对象** —— 表 / 函数 / 列 / 授权一律不存在。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pg_support
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "alembic" / "versions" / "0001_initial_schema.py"

PG_ONLY = pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)

#: 必须存在的关键表（覆盖核心 / 认证 / 摄取 / 教学 / 知识库各层）。
EXPECTED_TABLES = {
    "tenants",
    "principals",
    "projects",
    "project_grants",
    "user_sessions",
    "account_credentials",
    "platform_budget_config",
    "platform_paid_reservations",
    "auth_attempt_counters",
    "auth_audit_outbox",
    "ingestion_jobs",
    "source_documents",
    "source_chunks",
    "source_fetch_artifacts",
    "acquisition_jobs",
    "acquisition_fetch_observations",
    "teaching_runs",
    "provider_attempts",
    "library_sources",
    "library_documents",
}

#: 必须存在的 definer 函数（签名按 `pg_proc` 的 regprocedure 写法）。
EXPECTED_FUNCTIONS = (
    "public.register_auth_attempt(text,integer)",
    "public.auth_audit_pending_count()",
    "public.register_account(text,text,text,integer,timestamp with time zone)",
    "public.lookup_account_for_login(text)",
    "public.complete_account_login(text,bigint,timestamp with time zone,text,integer)",
    "public.study_metrics_snapshot()",
    "public.learning_jsonb_string_array(jsonb)",
    "public.reserve_platform_paid(text,date,text,text,bigint)",
    "public.settle_platform_paid(text,bigint)",
)

#: worker 凭据边界的三个策略必须绑定到 `study_worker` 角色。
WORKER_POLICIES = {
    "ingestion_jobs": "ingestion_jobs_worker",
    "acquisition_jobs": "acquisition_jobs_worker",
    "teaching_runs": "teaching_runs_worker",
}


def _run_alembic(dsn: str, *arguments: str, check: bool = True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "STUDY_PLATFORM_MIGRATION_DSN": dsn},
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError((result.stdout or "") + (result.stderr or ""))
    return result


def test_baseline_source_has_no_invitation_objects() -> None:
    """基线源码里不得残留任何邀请码对象（不依赖数据库）。"""
    assert BASELINE.is_file(), "baseline migration is not implemented"
    source = BASELINE.read_text(encoding="utf-8")
    lowered = source.lower()

    assert 'revision = "0001"' in lowered
    assert "down_revision = none" in lowered
    for forbidden in (
        "invitations",
        "invitee_principal_id",
        "exchange_invitation",
        "auth_method = 'invitation'",
    ):
        assert forbidden not in lowered, f"baseline still references {forbidden!r}"


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_fresh_database_upgrades_once_into_the_baseline_schema() -> None:
    """空库一次 `upgrade head` 成功，且关键表 / 函数 / 策略齐备。"""
    database = pg_support.create_test_database()
    try:
        dsn = database.migration_dsn
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "0001"

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public'"
                ).fetchall()
            }
            assert EXPECTED_TABLES <= tables, sorted(EXPECTED_TABLES - tables)

            for signature in EXPECTED_FUNCTIONS:
                assert conn.execute(
                    "SELECT to_regprocedure(%s) IS NOT NULL", (signature,)
                ).fetchone()[0] is True, signature

            # 快照函数的执行面：应用能调、worker 不能。
            assert conn.execute(
                "SELECT has_function_privilege('study_app',"
                " 'public.study_metrics_snapshot()', 'EXECUTE')"
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT has_function_privilege('study_worker',"
                " 'public.study_metrics_snapshot()', 'EXECUTE')"
            ).fetchone()[0] is False

            # 平台额度配置的种子行存在（且只有一行）。
            assert conn.execute(
                "SELECT count(*) FROM public.platform_budget_config"
            ).fetchone()[0] == 1

            # ---- 没有任何邀请码对象 -----------------------------------
            assert conn.execute(
                "SELECT to_regclass('public.invitations') IS NULL"
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT to_regprocedure('public.exchange_invitation(text,text,timestamptz)')"
                " IS NULL"
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'invitee_principal_id'"
            ).fetchone()[0] == 0

            # ---- worker 策略按角色绑定（不是靠自定义 GUC） -------------
            for table, policy in WORKER_POLICIES.items():
                roles = conn.execute(
                    "SELECT roles FROM pg_policies"
                    " WHERE schemaname = 'public' AND tablename = %s AND policyname = %s",
                    (table, policy),
                ).fetchone()
                assert roles is not None, f"{policy} 不存在"
                assert list(roles[0]) == ["study_worker"], (policy, roles[0])

            # ---- 权限边界：应用不能推进队列、也碰不到凭据表 ------------
            assert conn.execute(
                "SELECT has_table_privilege('study_app','public.ingestion_jobs','UPDATE')"
            ).fetchone()[0] is False
            assert conn.execute(
                "SELECT has_table_privilege('study_worker','public.ingestion_jobs','UPDATE')"
            ).fetchone()[0] is True
            for role in ("study_app", "study_worker"):
                assert conn.execute(
                    "SELECT has_table_privilege(%s, 'public.account_credentials', 'SELECT')",
                    (role,),
                ).fetchone()[0] is False
    finally:
        pg_support.drop_test_database(database)


def _register_account() -> tuple[str, str, str]:
    """用真实的注册 definer 函数造一个账号，返回 (tenant, principal, session)。"""
    suffix = uuid.uuid4().hex[:8]
    username = "init" + suffix
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    with psycopg.connect(pg_support.app_dsn()) as conn:
        row = conn.execute(
            "SELECT * FROM public.register_account(%s, %s, %s, 1, %s)",
            (
                username,
                username,
                "$argon2id$v=19$m=19456,t=2,p=1$test$hash",
                expires_at,
            ),
        ).fetchone()
    assert row is not None
    return row[2], row[3], row[1]


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_baseline_rls_denies_missing_context_and_isolates_principals() -> None:
    """缺上下文 / 错主体都必须退化成 0 行，正确上下文才可见。"""
    database = pg_support.create_test_database()
    try:
        tenant_id, principal_id, session_id = _register_account()

        # 正确上下文：看得到自己的会话。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            conn.execute("SELECT set_config('app.principal_id', %s, true)", (principal_id,))
            assert conn.execute(
                "SELECT count(*) FROM user_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()[0] == 1

        # 缺上下文：0 行（不是报错，也不是全部）。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            assert conn.execute("SELECT count(*) FROM user_sessions").fetchone()[0] == 0

        # 同租户的另一个主体：0 行。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            conn.execute("SELECT set_config('app.principal_id', 'u_stranger', true)")
            assert conn.execute(
                "SELECT count(*) FROM user_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()[0] == 0

        # 应用角色设了 worker 变量也读不到队列（凭据边界在角色上）。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            conn.execute("SELECT set_config('app.worker_id', 'forged', true)")
            assert conn.execute("SELECT count(*) FROM ingestion_jobs").fetchone()[0] == 0

        # append-only：应用对知识库没有 UPDATE 权限。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE library_sources SET display_name = display_name")
    finally:
        pg_support.drop_test_database(database)
