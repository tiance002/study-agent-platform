"""用户级共享知识库的 PostgreSQL 专项测试。

守住内存版覆盖不到的三条：

1. **FORCE RLS 真的在生效**：`study_app` 在无上下文 / 错主体上下文下
   读 `library_sources` 得到 **0 行**（不是"读到别人的"，也不是报错）；
2. **append-only 由 GRANT 保证**：应用角色对这两张表没有 `UPDATE`；
3. **迁移往返**：`0021` 在随机临时库上 `downgrade → upgrade` 往返，
   表与策略都真的消失又回来。

外加一条端到端：注册用户 → 登记知识库 → 关联到默认项目 → worker 摄取 →
既有检索命中。用真实 cookie 会话与真实 PG 适配器，不绕过认证。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pg_support
import psycopg
import pytest
from app.deployment import DeploymentSettings
from app.identity.ports import SystemContext
from app.main import build_platform, create_app
from app.workers.ingestion import run_once as run_ingestion_once
from fastapi.testclient import TestClient

ROOT = pg_support.ROOT

MARKDOWN = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。\n"


def _reachable() -> bool:
    return pg_support.reachable()


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"),
]


def _settings() -> DeploymentSettings:
    return DeploymentSettings.load(
        {
            "STUDY_PLATFORM_PERSISTENCE": "postgres",
            "STUDY_PLATFORM_REGISTRATION_ENABLED": "true",
            "STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED": "true",
            "STUDY_PLATFORM_EXCHANGE_LIMIT": "100000",
        }
    )


def _register(client: TestClient, *, suffix: str) -> tuple[str, str]:
    """注册一个用户，返回 `(principal_id, default_project_id)`。"""
    username = "lib" + suffix
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "Library12"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["default_project_id"]
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        row = conn.execute(
            "SELECT principal_id FROM project_grants WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        tenant_id = conn.execute(
            "SELECT tenant_id FROM projects WHERE project_id = %s", (project_id,)
        ).fetchone()
    assert row is not None and tenant_id is not None
    return row[0], project_id


def _keyed(key: str | None = None) -> dict:
    return {
        "Origin": "http://testserver",
        "Idempotency-Key": key or "libpg-" + uuid.uuid4().hex,
    }


def test_library_end_to_end_in_postgres(tmp_path):
    """登记 → 关联 → 摄取 → 检索：真实 PG 适配器 + 真实 cookie 会话。"""
    suffix = uuid.uuid4().hex[:10]
    platform = build_platform(var_dir=tmp_path / "web", settings=_settings())
    client = TestClient(create_app(platform=platform))
    principal_id, project_id = _register(client, suffix=suffix)

    registered = client.post(
        "/library/sources",
        json={
            "display_name": "事务讲义",
            "media_type": "text/markdown",
            "acquisition": {"kind": "upload", "n": suffix},
            "content": MARKDOWN,
        },
        headers=_keyed(),
    )
    assert registered.status_code == 201, registered.text
    library_source_id = registered.json()["library_source_id"]
    assert registered.json()["has_content"] is True
    assert registered.json()["identity_hash"].startswith("sha256:")

    # 同样的 acquisition 再登记一次：返回既有记录（200），库里仍只有一行。
    repeated = client.post(
        "/library/sources",
        json={
            "display_name": "事务讲义(重传)",
            "media_type": "text/markdown",
            "acquisition": {"kind": "upload", "n": suffix},
        },
        headers=_keyed(),
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["library_source_id"] == library_source_id
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM library_sources WHERE principal_id = %s", (principal_id,)
        ).fetchone()[0] == 1

    attached = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(),
    )
    assert attached.status_code == 200, attached.text
    body = attached.json()
    assert body["created"] is True and body["ingestion_job_id"]
    job_id = body["ingestion_job_id"]

    for _ in range(50):
        if run_ingestion_once(platform, worker_id="lib-pg").kind == "idle":
            break
    job = client.get(f"/projects/{project_id}/ingestion-jobs/{job_id}").json()
    assert job["status"] == "succeeded", job

    hits = client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "limit": 5},
        headers={"Origin": "http://testserver"},
    ).json()["hits"]
    assert hits, "关联进来的原文必须落到项目级检索里"
    assert hits[0]["citation"]["source_id"] == body["source_id"]

    # 重复关联：复用既有资料与既有摄取任务。
    again = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(),
    )
    assert again.status_code == 200, again.text
    assert again.json() == {**body, "created": False}
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM sources WHERE project_id = %s", (project_id,)
        ).fetchone()[0] == 1


def test_library_source_is_invisible_to_another_principal_in_postgres(tmp_path):
    suffix = uuid.uuid4().hex[:10]
    platform = build_platform(var_dir=tmp_path / "web", settings=_settings())
    owner_client = TestClient(create_app(platform=platform))
    _register(owner_client, suffix=suffix)
    intruder_client = TestClient(create_app(platform=platform))
    _register(intruder_client, suffix="b" + suffix)

    registered = owner_client.post(
        "/library/sources",
        json={"display_name": "私有讲义", "acquisition": {"kind": "upload", "n": suffix}},
        headers=_keyed(),
    )
    library_source_id = registered.json()["library_source_id"]

    assert intruder_client.get("/library/sources").json()["sources"] == []
    upload = intruder_client.post(
        f"/library/sources/{library_source_id}/content",
        json={"content": MARKDOWN},
        headers=_keyed(),
    )
    assert upload.status_code == 404, upload.text
    assert upload.json()["code"] == "NOT_FOUND"
    assert upload.json()["message"] == "资源不存在"

    # 正向对照：所有者自己看得到 —— 否则上面那条通过得毫无意义。
    assert owner_client.get("/library/sources").json()["sources"][0][
        "library_source_id"
    ] == library_source_id


def test_attach_requires_membership_in_postgres(tmp_path):
    suffix = uuid.uuid4().hex[:10]
    platform = build_platform(var_dir=tmp_path / "web", settings=_settings())
    client = TestClient(create_app(platform=platform))
    _principal_id, project_id = _register(client, suffix=suffix)

    registered = client.post(
        "/library/sources",
        json={"display_name": "讲义", "acquisition": {"kind": "upload", "n": suffix}},
        headers=_keyed(),
    )
    library_source_id = registered.json()["library_source_id"]

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        tenant_id = conn.execute(
            "SELECT tenant_id FROM projects WHERE project_id = %s", (project_id,)
        ).fetchone()[0]
    ungranted = "proj_lib_nogrant_" + suffix
    platform.membership.create_project(
        SystemContext(tenant_id, "PG 知识库测试"), project_id=ungranted, name="未授权"
    )

    denied = client.post(
        f"/projects/{ungranted}/library-sources/{library_source_id}/attach", headers=_keyed()
    )
    assert denied.status_code == 404, denied.text
    assert denied.json()["code"] == "NOT_FOUND"


@pytest.mark.invariant
def test_study_app_sees_no_library_rows_without_context(pg_database):
    """RLS 反例：无上下文 0 行、错主体 0 行、正确主体能看到自己那一行。"""
    suffix = uuid.uuid4().hex[:10]
    tenant_id = "t_lib_rls_" + suffix
    owner_id = "u_lib_rls_owner_" + suffix
    other_id = "u_lib_rls_other_" + suffix
    library_source_id = "libsrc_rls_" + suffix

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, 'rls')", (tenant_id,)
            )
            for principal in (owner_id, other_id):
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
                    (principal, tenant_id),
                )
            conn.execute(
                "INSERT INTO library_sources (library_source_id, tenant_id, principal_id,"
                " display_name, media_type, identity_hash, acquisition)"
                " VALUES (%s, %s, %s, 'rls 资料', 'text/markdown', %s, '{}'::jsonb)",
                (library_source_id, tenant_id, owner_id, "sha256:" + suffix),
            )

    def _count(*, tenant: str | None, principal: str | None) -> int:
        with psycopg.connect(pg_support.app_dsn()) as conn, conn.transaction():
            if tenant is not None:
                conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            if principal is not None:
                conn.execute("SELECT set_config('app.principal_id', %s, true)", (principal,))
            return conn.execute("SELECT count(*) FROM library_sources").fetchone()[0]

    # 缺上下文 → 0 行（不是"全部"）。
    assert _count(tenant=None, principal=None) == 0, "无上下文必须看到 0 行"
    assert _count(tenant=tenant_id, principal=None) == 0, "缺主体上下文必须是 0 行"
    # 同租户的另一个主体 → 0 行。这条正是"用户级"与"租户级"的分界。
    assert _count(tenant=tenant_id, principal=other_id) == 0, "同租户的别人不该看到"
    # 正向对照：所有者自己看得到。
    assert _count(tenant=tenant_id, principal=owner_id) == 1


@pytest.mark.invariant
def test_app_role_cannot_update_library_rows(pg_database):
    """append-only 由 GRANT 保证：应用角色没有 `UPDATE` / `DELETE`。"""
    for statement in (
        "UPDATE library_sources SET display_name = display_name",
        "DELETE FROM library_sources",
        "UPDATE library_documents SET content = content",
        "DELETE FROM library_documents",
    ):
        # 每条语句一条新连接：被权限拒绝的语句会让当前事务中止，
        # 复用同一条连接会以"事务已中止"而不是"权限不足"失败。
        with psycopg.connect(pg_support.app_dsn()) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def _run_alembic(dsn: str, *arguments: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "STUDY_PLATFORM_MIGRATION_DSN": dsn},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_0021_migration_cycle_is_reversible(pg_database):
    """`0021` 在随机临时库上往返：表与策略真的消失又回来。"""
    database = pg_support.create_test_database()
    try:
        with psycopg.connect(database.migration_dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_name IN ('library_sources', 'library_documents')"
            ).fetchone()[0] == 2

        _run_alembic(database.migration_dsn, "downgrade", "0019")
        with psycopg.connect(database.migration_dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_name IN ('library_sources', 'library_documents')"
            ).fetchone()[0] == 0
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == "0019"

        _run_alembic(database.migration_dsn, "upgrade", "head")
        with psycopg.connect(database.migration_dsn) as conn:
            rows = conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
                "  FROM pg_class c JOIN pg_policy p ON p.polrelid = c.oid"
                " WHERE c.relname IN ('library_sources', 'library_documents')"
            ).fetchall()
            assert sorted(rows) == [
                ("library_documents", True, True),
                ("library_sources", True, True),
            ]
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0021"
    finally:
        pg_support.drop_test_database(database)
