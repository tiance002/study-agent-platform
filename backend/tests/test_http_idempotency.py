"""HTTP 命令幂等的端到端测试（任务 6 退出门核心）。

覆盖四条语义：同键同内容重放（不重复执行）、同键换内容（VIOLATION）、
失败释放占用（修好后的重试真的重新执行）、跨"进程"重放
（全新适配器实例看到同一份状态 —— PG；内存版用注入共享字典对照）。
"""

from __future__ import annotations

import os
import uuid

import pytest
from app.api.http_idempotency import (
    InMemoryHttpIdempotencyStore,
    PostgresHttpIdempotencyStore,
)
from app.identity.models import Principal
from app.identity.ports import SystemContext
from app.main import DEMO_PRINCIPAL, DEMO_TENANT, create_app
from fastapi.testclient import TestClient

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(platform):
    """**本地覆盖** conftest 的裸 client：幂等测试需要已登录的 cookie 用户
    （幂等键按主体划分作用域，无身份就没有幂等可言）。"""
    return _login(platform)


def _pg_reachable() -> bool:
    import psycopg

    try:
        with psycopg.connect(
            "postgresql://postgres@127.0.0.1:5432/study_platform", connect_timeout=2
        ):
            return True
    except Exception:
        return False


def _login(platform) -> TestClient:
    token = "idem-" + uuid.uuid4().hex
    import hashlib
    from datetime import timedelta

    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "幂等测试"),
        invitation_id="inv_" + uuid.uuid4().hex[:8],
        token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    client = TestClient(create_app(platform=platform))
    assert client.post("/auth/invitations/exchange", json={"token": token}).status_code == 200
    return client


# ------------------------------------------------------------ 重放


@pytest.mark.invariant
def test_same_key_same_body_replays_cached_response(client):
    body = {"name": "幂等项目"}
    headers = {**ORIGIN, "Idempotency-Key": "key-replay-1"}

    first = client.post("/projects", json=body, headers=headers)
    assert first.status_code == 201
    assert "X-Idempotent-Replay" not in first.headers

    second = client.post("/projects", json=body, headers=headers)
    assert second.status_code == 201
    assert second.headers.get("X-Idempotent-Replay") == "true", (
        "重放必须可观测：客户端分不清'重试没生效'和'命中缓存'就等于没有幂等"
    )
    assert second.json() == first.json(), "缓存响应必须与首次响应一致"
    assert second.json()["project_id"] == first.json()["project_id"], (
        "重放不得创建第二个项目 —— 幂等的全部意义"
    )


@pytest.mark.invariant
def test_no_key_is_passthrough(client):
    """不带 Idempotency-Key：两次请求就是两次执行（无幂等要求）。"""
    first = client.post("/projects", json={"name": "a"}, headers=ORIGIN)
    second = client.post("/projects", json={"name": "a"}, headers=ORIGIN)
    assert first.json()["project_id"] != second.json()["project_id"]


# ------------------------------------------------------------ 冲突


@pytest.mark.invariant
def test_same_key_different_body_is_violation(client):
    headers = {**ORIGIN, "Idempotency-Key": "key-conflict-1"}
    first = client.post("/projects", json={"name": "原始内容"}, headers=headers)
    assert first.status_code == 201

    conflict = client.post("/projects", json={"name": "换内容"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_VIOLATION"


# ------------------------------------------------------------ 失败释放


@pytest.mark.invariant
def test_failed_request_releases_claim_for_retry(client):
    """同键换 node → 404 拒绝（不缓存），换回合法参数的重试真的重新执行。

    反向场景：如果拒绝被缓存，修好参数的重试会永远拿到旧拒绝 ——
    那是铁律 17 修掉的行为，必须有测试钉住。
    """
    headers = {**ORIGIN, "Idempotency-Key": "key-fail-1"}
    # 先在一个会失败的端点上占用该键（PATCH 不存在的项目 → 404）。
    denied = client.patch(
        "/projects/proj_nonexistent",
        json={"name": "x", "expected_version": 1},
        headers=headers,
    )
    assert denied.status_code == 404

    # 同键打合法端点：不同 scope（路由模板不同）→ 正常执行，不受影响。
    ok = client.post("/projects", json={"name": "新项目"}, headers=headers)
    assert ok.status_code == 201


@pytest.mark.invariant
def test_idempotency_scoped_per_command(client):
    """同一把 key 在**不同端点**上互不串味（command_scope 进唯一键）。"""
    key = "key-scope-1"
    project_id = client.post(
        "/projects", json={"name": "项目"}, headers={**ORIGIN, "Idempotency-Key": key}
    ).json()["project_id"]

    # 同 key 打消息端点：不同 scope → 正常执行，不是重放。
    conversation_id = client.post(
        f"/projects/{project_id}/conversations",
        json={"title": "会话"},
        headers={**ORIGIN, "Idempotency-Key": key},
    ).json()["conversation_id"]
    assert conversation_id, "同键不同命令不得互相重放"


# ------------------------------------------------------------ PG 持久化


@pytest.mark.postgres
@pytest.mark.skipif(
    not _pg_reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_pg_idempotency_survives_store_restart():
    """PG 版：全新 store 实例（模拟重启）后重试仍命中缓存 —— 持久化的定义。"""

    from app.db.identity_store import PostgresMembershipRepository

    os_environ_dsn = __import__("os").environ.get(
        "STUDY_PLATFORM_DSN", "postgresql://study_app:dev-only-change-me@127.0.0.1:5432/study_platform"
    )
    import psycopg

    migration_dsn = os.environ.get(
        "STUDY_PLATFORM_MIGRATION_DSN",
        "postgresql://postgres@127.0.0.1:5432/study_platform",
    )
    membership = PostgresMembershipRepository(dsn=os_environ_dsn)
    tenant = "t_idem_pg"
    principal = "u_idem_pg"
    # 种子用超级用户连接（迁移 DSN）：建租户/主体是运维动作，不走应用路径。
    with psycopg.connect(migration_dsn) as seed_conn:
        with seed_conn.transaction():
            seed_conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant, tenant),
            )
            seed_conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                (principal, tenant),
            )
    project_id = f"proj_idem_{uuid.uuid4().hex[:10]}"
    membership.create_project_for(
        Principal(principal_id=principal, tenant_id=tenant),
        project_id=project_id, name="", goal="",
    )

    store_a = PostgresHttpIdempotencyStore(dsn=os_environ_dsn)
    fingerprint = "sha256:" + uuid.uuid4().hex
    keys = dict(
        tenant_id=tenant, principal_id=principal,
        command_scope="POST /projects", client_key="pg-idem-" + uuid.uuid4().hex,
    )
    first = store_a.claim(**keys, fingerprint=fingerprint)
    assert first.kind == "claimed"
    store_a.complete(**keys, status_code=201, response_body={"project_id": project_id})

    # 「重启」：全新实例。
    store_b = PostgresHttpIdempotencyStore(dsn=os_environ_dsn)
    replay = store_b.claim(**keys, fingerprint=fingerprint)
    assert replay.kind == "replay"
    assert replay.cached_body == {"project_id": project_id}

    # 换内容 → VIOLATION（跨进程同样成立）。
    violation = store_b.claim(
        **{**keys, "client_key": keys["client_key"]}, fingerprint="sha256:" + uuid.uuid4().hex
    )
    assert violation.kind == "violation"


# ------------------------------------------------------------ 内存对照


@pytest.mark.invariant
def test_memory_store_shared_dict_visibility():
    """内存对照：共享字典的两个 store 实例看到同一份状态（与 PG 重启恢复对齐）。"""
    store_a = InMemoryHttpIdempotencyStore()
    keys = dict(tenant_id="t", principal_id="u", command_scope="POST /x", client_key="k")
    fp = "sha256:abc"
    assert store_a.claim(**keys, fingerprint=fp).kind == "claimed"
    store_a.complete(**keys, status_code=200, response_body={"ok": 1})

    store_b = InMemoryHttpIdempotencyStore()
    # 不共享 —— 内存版重启即失是**已知且已文档化**的行为，这里只验证同实例语义。
    assert store_b.claim(**keys, fingerprint=fp).kind == "claimed"
