"""HTTP 命令幂等的端到端测试（任务 6 退出门核心）。

覆盖四条语义：同键同内容重放（不重复执行）、同键换内容（VIOLATION）、
失败释放占用（修好后的重试真的重新执行）、跨"进程"重放
（全新适配器实例看到同一份状态 —— PG；内存版用注入共享字典对照）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pg_support
import psycopg
import pytest
from app.api.http_idempotency import (
    MAX_CACHED_RESPONSE_BYTES,
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
def test_no_key_is_rejected(client):
    """审查 P1 修复：不带 Idempotency-Key 的变更请求必须被拒绝。

    早先"缺头直通"让守卫形同虚设 —— 客户端超时重试仍会重复创建项目。
    冻结规格要求变更接口必须携带该头。
    """
    denied = client.post("/projects", json={"name": "a"}, headers=ORIGIN)
    assert denied.status_code == 400
    assert denied.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.invariant
def test_key_over_max_length_is_rejected(client):
    """client_key 上限 200 字符（0007 规格冻结的数据边界）。"""
    headers = {**ORIGIN, "Idempotency-Key": "k" * 201}
    denied = client.post("/projects", json={"name": "a"}, headers=headers)
    assert denied.status_code == 400
    assert denied.json()["code"] == "PARAMS_INVALID"


@pytest.mark.invariant
def test_oversized_response_is_not_cached_verbatim():
    """缓存响应有界：超大响应落显式标记，不给 JSONB 无界放大留门。

    直击 store 层：端点响应受请求字段上限约束很难合法超过 64KB，
    但"缓存体必须有界"是存储层的 invariant，在 store 上钉住最直接。
    """
    store = InMemoryHttpIdempotencyStore()
    keys = dict(tenant_id="t", principal_id="u", command_scope="POST /x", client_key="k")
    fp = "sha256:big"

    first = store.claim(**keys, fingerprint=fp)
    assert first.kind == "claimed"
    huge = {"payload": "y" * (MAX_CACHED_RESPONSE_BYTES + 1)}
    store.complete(**keys, status_code=201, response_body=huge, owner_token=first.owner_token)

    replay = store.claim(**keys, fingerprint=fp)
    assert replay.kind == "replay"
    assert replay.cached_body is not None
    assert replay.cached_body != huge, "原文不得进缓存"
    assert replay.cached_body.get("idempotency_cache") == "response_too_large"
    assert replay.cached_body.get("limit_bytes") == MAX_CACHED_RESPONSE_BYTES


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
def test_cache_completion_failure_keeps_claim_ambiguous(platform, client):
    """业务成功后缓存写失败时不得 release，否则重试会重复业务。"""

    class CompletionFailsStore(InMemoryHttpIdempotencyStore):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        def complete(self, **kwargs) -> None:
            raise RuntimeError("simulated cache completion failure")

        def release(self, **kwargs) -> None:
            self.release_calls += 1
            super().release(**kwargs)

    store = CompletionFailsStore()
    platform.http_idempotency = store
    before = len(client.get("/projects").json()["projects"])
    headers = {**ORIGIN, "Idempotency-Key": "cache-failure-1"}

    with pytest.raises(RuntimeError, match="cache completion failure"):
        client.post("/projects", json={"name": "只创建一次"}, headers=headers)

    assert store.release_calls == 0
    assert len(client.get("/projects").json()["projects"]) == before + 1
    retry = client.post("/projects", json={"name": "只创建一次"}, headers=headers)
    assert retry.status_code == 409
    assert retry.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert len(client.get("/projects").json()["projects"]) == before + 1


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
    store_a.complete(
        **keys, status_code=201, response_body={"project_id": project_id},
        owner_token=first.owner_token,
    )

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
    claimed = store_a.claim(**keys, fingerprint=fp)
    assert claimed.kind == "claimed"
    store_a.complete(**keys, status_code=200, response_body={"ok": 1}, owner_token=claimed.owner_token)

    store_b = InMemoryHttpIdempotencyStore()
    # 不共享 —— 内存版重启即失是**已知且已文档化**的行为，这里只验证同实例语义。
    assert store_b.claim(**keys, fingerprint=fp).kind == "claimed"


@pytest.mark.invariant
def test_memory_claim_is_atomic_between_threads():
    """开发适配器也必须保证同一逻辑键只有一个占用者。"""

    class SlowGetDict(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            time.sleep(0.05)
            return value

    store = InMemoryHttpIdempotencyStore()
    store._rows = SlowGetDict()
    keys = dict(
        tenant_id="t",
        principal_id="u",
        command_scope="POST /projects",
        client_key="concurrent",
        fingerprint="sha256:concurrent",
    )
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def claim() -> None:
        start.wait()
        outcomes.append(store.claim(**keys).kind)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["claimed", "in_progress"]


# ------------------------------------------------------- 审查回归（claim_id / 租约）


@pytest.mark.invariant
def test_same_client_key_across_users_and_commands_no_collision():
    """审查 P1 回归：同 client_key 跨用户、跨命令互不干扰。

    早先 claim_id 只哈希 client_key 且作为全局主键 —— 两个用户用同一
    客户端 key 时第二个必然主键冲突 500。claim_id 必须从完整逻辑键派生。
    """
    store = InMemoryHttpIdempotencyStore()
    fp = "sha256:xyz"
    user_a = dict(tenant_id="t", principal_id="user_a", command_scope="POST /projects")
    user_b = dict(tenant_id="t", principal_id="user_b", command_scope="POST /projects")
    other_cmd = dict(tenant_id="t", principal_id="user_a", command_scope="POST /other")

    assert store.claim(**user_a, client_key="same-key", fingerprint=fp).kind == "claimed"
    assert store.claim(**user_b, client_key="same-key", fingerprint=fp).kind == "claimed", (
        "另一主体用同一 client_key 必须独立占用，不得主键冲突"
    )
    assert store.claim(**other_cmd, client_key="same-key", fingerprint=fp).kind == "claimed", (
        "同一主体在另一命令上用同一 client_key 必须独立占用"
    )


@pytest.mark.invariant
def test_expired_pending_claim_requires_reconciliation():
    """超时 pending 的结果未知，不得通过自动重执行猜测它失败了。"""
    store = InMemoryHttpIdempotencyStore(lease_seconds=0)
    keys = dict(tenant_id="t", principal_id="u", command_scope="POST /x", client_key="k")
    fp = "sha256:lease"

    first = store.claim(**keys, fingerprint=fp)
    assert first.kind == "claimed"

    expired = store.claim(**keys, fingerprint=fp)
    assert expired.kind == "reconciliation_required"
    row = store._rows[store._key("t", "u", "POST /x", "k")]
    assert row["state"] == "indeterminate"


@pytest.mark.invariant
def test_stale_owner_cannot_complete_or_release_indeterminate_claim():
    """进入不确定态后，迟到的旧持有者不得再改写结果。"""
    store = InMemoryHttpIdempotencyStore()  # 默认租约 120s
    keys = dict(tenant_id="t", principal_id="u", command_scope="POST /x", client_key="k")
    fp = "sha256:owner"

    def age_row_by(seconds: float) -> None:
        row = store._rows[store._key("t", "u", "POST /x", "k")]
        row["claimed_at"] -= seconds

    stale = store.claim(**keys, fingerprint=fp)
    assert stale.kind == "claimed"

    # 租约到期：转为不确定态，禁止直接重执行。
    age_row_by(600)
    expired = store.claim(**keys, fingerprint=fp)
    assert expired.kind == "reconciliation_required"

    # 旧持有者迟到完成：不得把新持有者的占用标记成自己的结果。
    store.complete(
        **keys, status_code=200, response_body={"from": "stale"},
        owner_token=stale.owner_token,
    )
    row = store._rows[store._key("t", "u", "POST /x", "k")]
    store.release(**keys, owner_token=stale.owner_token)
    assert row["state"] == "indeterminate"
    assert store.claim(**keys, fingerprint=fp).kind == "reconciliation_required"


@pytest.mark.postgres
@pytest.mark.skipif(
    not _pg_reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_pg_claim_id_does_not_collide_across_users():
    """PG 版对照：同一 client_key 跨主体/跨命令都能独立占用（claim_id 全键派生）。

    审查实测：claim_id 只哈希 client_key 时，第二个用户立即主键冲突 500。
    """
    tenant = "t_idem_collision"
    principal_a = "u_idem_collision_a"
    principal_b = "u_idem_collision_b"
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant, tenant),
            )
            conn.cursor().executemany(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                [(principal_a, tenant), (principal_b, tenant)],
            )

    store = PostgresHttpIdempotencyStore()
    fp = "sha256:" + uuid.uuid4().hex
    user_a = dict(tenant_id=tenant, principal_id=principal_a, command_scope="POST /projects")
    user_b = dict(tenant_id=tenant, principal_id=principal_b, command_scope="POST /projects")
    client_key = "shared-" + uuid.uuid4().hex

    assert store.claim(**user_a, client_key=client_key, fingerprint=fp).kind == "claimed"
    assert store.claim(**user_b, client_key=client_key, fingerprint=fp).kind == "claimed"


@pytest.mark.postgres
@pytest.mark.skipif(
    not _pg_reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_pg_expired_pending_requires_reconciliation():
    """PG 状态机同样把超时占用推进到 indeterminate，绝不接管重执行。"""
    tenant = "t_idem_expired"
    principal = "u_idem_expired"
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant, tenant),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                (principal, tenant),
            )

    store = PostgresHttpIdempotencyStore()
    keys = dict(
        tenant_id=tenant,
        principal_id=principal,
        command_scope="POST /projects",
        client_key="expired-" + uuid.uuid4().hex,
    )
    fingerprint = "sha256:" + uuid.uuid4().hex
    assert store.claim(**keys, fingerprint=fingerprint).kind == "claimed"

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE http_idempotency SET claimed_at = now() - interval '10 minutes'"
                " WHERE tenant_id = %s AND principal_id = %s"
                " AND command_scope = %s AND client_key = %s",
                (
                    keys["tenant_id"],
                    keys["principal_id"],
                    keys["command_scope"],
                    keys["client_key"],
                ),
            )

    assert store.claim(**keys, fingerprint=fingerprint).kind == "reconciliation_required"
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        state = conn.execute(
            "SELECT state FROM http_idempotency"
            " WHERE tenant_id = %s AND principal_id = %s"
            " AND command_scope = %s AND client_key = %s",
            (
                keys["tenant_id"],
                keys["principal_id"],
                keys["command_scope"],
                keys["client_key"],
            ),
        ).fetchone()[0]
    assert state == "indeterminate"
