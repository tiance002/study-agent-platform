"""第 1 轮安全收口的 PostgreSQL 侧测试。

只在本地 PostgreSQL 运行时收集；**skip 不算通过**（退出门要求 PG 实测）。
覆盖：

- 0004 迁移的会话 TTL CHECK 约束（100 年会话插不进去）；
- 适配器把 CheckViolation 翻译成 INTERNAL_CONSISTENCY_ERROR（不泄露约束名）；
- `register_auth_attempt` SECURITY DEFINER 限流函数：窗口计数、跨窗口重置；
- 应用角色对限流表**零表权限**（只能走 definer 函数）；
- 启动自检：连不上 / 迁移版本不符 → 启动失败；
- 生产形态 HTTP 闭环：兑换 → 重启新进程 → cookie 仍有效；bearer 关闭；
  严格 CSRF 在 PG 形态同样拒绝无来源请求。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.ports import SystemContext
from app.main import build_platform, create_app
from fastapi.testclient import TestClient

# 本地库 trust 认证：密码会被服务器忽略，但**不能省略** ——
# 省了密码这个字符串就与 db.settings.DEFAULT_APP_DSN 完全相等，
# 会撞上生产启动自检的「禁止本机开发默认值」判定（该判定是正确行为）。
# dev-only 标记让秘密扫描放行（项目红线：明文凭据必须带占位标记）。


def _reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"),
]

TENANT = "t_hard_pg"
PRINCIPAL = "u_hard_pg"


@pytest.fixture(scope="module", autouse=True)
def seed_and_clean():
    """超级用户播种租户/主体，并清空限流计数桶（definer 函数表应用角色碰不到）。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (TENANT, TENANT),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                (PRINCIPAL, TENANT),
            )
            conn.execute("TRUNCATE auth_attempt_counters")
    yield


def _hash(raw: str) -> str:
    from hashlib import sha256

    return f"sha256:{sha256(raw.encode()).hexdigest()}"


def _issue_invitation(platform, token: str) -> None:
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(TENANT, "加固测试"),
        invitation_id="inv_" + uuid.uuid4().hex,
        token_hash=_hash(token),
        issued_by=PRINCIPAL,
        invitee_principal_id=PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )


# ------------------------------------------------------- TTL 约束


@pytest.mark.invariant
def test_session_ttl_check_constraint_rejects_100_year_session():
    """数据库层硬约束：超过 30 天的会话期限由 CHECK 直接拒绝。"""
    from app.db.identity_store import PostgresSessionRepository

    store = PostgresSessionRepository()
    now = datetime.now(timezone.utc)
    from app.product.models import UserSession

    ancient = UserSession(
        session_id="sess_" + uuid.uuid4().hex,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=36500),
    )
    with pytest.raises(PlatformError) as excinfo:
        store.create(ancient)
    assert excinfo.value.code is ErrorCode.INTERNAL_CONSISTENCY_ERROR

    # 约束在库里是具名的 0004 约束（迁移可审计）。
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        name = conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname = 'user_sessions_ttl_check'"
        ).fetchone()
    assert name is not None


@pytest.mark.invariant
def test_pg_exchange_with_huge_ttl_is_translated_and_invitation_intact(tmp_path):
    """兑换路径传入越界 TTL：definer 函数守卫 + 适配器翻译，邀请保持可兑换。"""
    from app.deployment import DeploymentSettings

    settings = DeploymentSettings.load({"STUDY_PLATFORM_PERSISTENCE": "postgres"})
    platform = build_platform(var_dir=tmp_path, settings=settings)
    token = "ttl-token-" + uuid.uuid4().hex
    _issue_invitation(platform, token)
    now = platform.clock.now()

    with pytest.raises(PlatformError) as excinfo:
        platform.invitations.exchange(
            _hash(token),
            session_id="sess_bad_" + uuid.uuid4().hex,
            session_expires_at=now + timedelta(days=36500),
        )
    assert excinfo.value.code is ErrorCode.INTERNAL_CONSISTENCY_ERROR

    # 合法期限兑换成功 —— 邀请没有被悬空消费。
    session = platform.invitations.exchange(
        _hash(token),
        session_id="sess_ok_" + uuid.uuid4().hex,
        session_expires_at=now + timedelta(hours=8),
    )
    assert session is not None


# ------------------------------------------------------- 限流函数与权限


@pytest.mark.invariant
def test_register_auth_attempt_function_counts_and_rolls_over():
    with psycopg.connect(pg_support.app_dsn()) as conn:
        bucket = "bucket_" + uuid.uuid4().hex
        with conn.transaction():
            counts = [
                conn.execute("SELECT register_auth_attempt(%s, %s)", (bucket, 600)).fetchone()[0]
                for _ in range(3)
            ]
        # 同一窗口内单调递增。
        assert counts == [1, 2, 3]
        # 另一个窗口（窗口长度 1 秒）立刻落在新桶。
        rolled = conn.execute(
            "SELECT register_auth_attempt(%s, %s)", (bucket, 1)
        ).fetchone()[0]
        assert rolled == 1


@pytest.mark.invariant
def test_app_role_has_no_table_privileges_on_rate_limit_table():
    """限流表是认证前设施：应用角色零表权限，只能调 definer 函数。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        privs = conn.execute(
            """
            SELECT privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'study_app' AND table_name = 'auth_attempt_counters'
            """
        ).fetchall()
    assert privs == []

    # 直接读/写必须被拒（RLS 无策略 + 无表权限双保险）。
    with psycopg.connect(pg_support.app_dsn()) as conn:
        for statement in (
            "SELECT * FROM auth_attempt_counters",
            "INSERT INTO auth_attempt_counters (bucket, window_start, attempts, last_at)"
            " VALUES ('x', now(), 1, now())",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            # 权限错误会中止当前事务；不回滚的话第二条语句只会报
            # InFailedSqlTransaction，把"第二条是否也被拒"这件事掩盖掉。
            conn.rollback()


@pytest.mark.invariant
def test_auth_audit_outbox_is_tenant_isolated():
    """带 tenant_id 的审计事实表必须受 FORCE RLS 保护。"""
    other_tenant = "t_hard_pg_other"
    event_mine = "aud_" + uuid.uuid4().hex
    event_other = "aud_" + uuid.uuid4().hex
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (other_tenant, other_tenant),
            )
            conn.execute(
                "INSERT INTO auth_audit_outbox"
                " (event_id, event_type, payload, risk, tenant_id)"
                " VALUES (%s, 'test', '{}'::jsonb, 'high', %s),"
                "        (%s, 'test', '{}'::jsonb, 'high', %s)",
                (event_mine, TENANT, event_other, other_tenant),
            )

    with psycopg.connect(pg_support.app_dsn()) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
        visible = conn.execute(
            "SELECT event_id FROM auth_audit_outbox"
            " WHERE event_id IN (%s, %s) ORDER BY event_id",
            (event_mine, event_other),
        ).fetchall()
        assert visible == [(event_mine,)]
        changed = conn.execute(
            "UPDATE auth_audit_outbox SET projected_at = now() WHERE event_id = %s",
            (event_other,),
        )
        assert changed.rowcount == 0


@pytest.mark.invariant
def test_two_audit_projectors_keep_one_valid_hash_chain(tmp_path):
    """两个 worker 各持独立 sink 缓存时，数据库锁仍必须保持单链。"""
    from app.audit.outbox import PostgresAuditOutbox
    from app.audit.sink import AuditSink

    event_ids = ["aud_" + uuid.uuid4().hex for _ in range(12)]
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute("TRUNCATE auth_audit_outbox")
            conn.cursor().executemany(
                "INSERT INTO auth_audit_outbox"
                " (event_id, event_type, payload, risk, tenant_id)"
                " VALUES (%s, 'projector_test', '{}'::jsonb, 'high', %s)",
                [(event_id, TENANT) for event_id in event_ids],
            )

    # 两个实例都在文件为空时构造，模拟两个 worker 各自缓存旧链头。
    sink_a = AuditSink(tmp_path / "audit")
    sink_b = AuditSink(tmp_path / "audit")
    outbox_a = PostgresAuditOutbox(sink_a, pg_support.app_dsn())
    outbox_b = PostgresAuditOutbox(sink_b, pg_support.app_dsn())
    threads = [
        threading.Thread(target=outbox_a.flush_pending, args=(TENANT,)),
        threading.Thread(target=outbox_b.flush_pending, args=(TENANT,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    reloaded = AuditSink(tmp_path / "audit")
    assert len(reloaded.read_all()) == len(event_ids)
    assert reloaded.verify_chain()


# ------------------------------------------------------- 启动自检


@pytest.mark.invariant
def test_startup_self_check_rejects_unreachable_and_wrong_version(tmp_path):
    from app.deployment import DeploymentSettings
    from app.main import _verify_database_ready

    # 连不上：启动失败而不是首个请求才 500。
    with pytest.raises(psycopg.OperationalError):
        _verify_database_ready(
            "postgresql://study_app@127.0.0.1:1/none", expected_version="0004"
        )

    # 版本不符：明确报错并指出实际/期望版本。
    with pytest.raises(RuntimeError) as excinfo:
        _verify_database_ready(pg_support.app_dsn(), expected_version="9999")
    assert "9999" in str(excinfo.value)

    # 生产配置缺失同样拒绝装配（fail-fast，不静默回退内存）。
    with pytest.raises(RuntimeError):
        build_platform(
            var_dir=tmp_path / "p",
            settings=DeploymentSettings.load({"STUDY_PLATFORM_ENV": "production"}),
        )


# ------------------------------------------------------- 生产形态 HTTP 闭环


def _postgres_settings(**overrides):
    from app.deployment import DeploymentSettings

    # 测试套件共享「测试客户端 IP」这一个限流桶，这里把限额调大，
    # 限流行为本身由专门的内存/PG 用例验证。
    env = {
        "STUDY_PLATFORM_PERSISTENCE": "postgres",
        "STUDY_PLATFORM_EXCHANGE_LIMIT": "100000",
        **overrides,
    }
    return DeploymentSettings.load(env)


@pytest.mark.invariant
def test_cookie_session_survives_process_restart_on_postgres(tmp_path):
    """进程 A 兑换 → 进程 B（全新装配，同一数据库）cookie 仍有效。"""
    token = "restart-token-" + uuid.uuid4().hex

    platform_a = build_platform(var_dir=tmp_path / "a", settings=_postgres_settings())
    _issue_invitation(platform_a, token)
    client_a = TestClient(create_app(platform=platform_a))
    exchanged = client_a.post("/auth/invitations/exchange", json={"token": token})
    assert exchanged.status_code == 200
    cookie = exchanged.cookies["study_session"]

    # 「重启」：全新进程对象，内存里什么都没有，状态只应来自 PostgreSQL。
    platform_b = build_platform(var_dir=tmp_path / "b", settings=_postgres_settings())
    client_b = TestClient(create_app(platform=platform_b))
    me = client_b.get("/me", cookies={"study_session": cookie})
    assert me.status_code == 200
    assert me.json()["principal_id"] == PRINCIPAL

    # 集中失效跨进程生效：A 退出所有设备后，B 手里的 cookie 立即失效。
    logout_all = client_a.post(
        "/auth/logout/all", headers={"Origin": "http://testserver"}
    )
    assert logout_all.status_code == 200
    assert client_b.get("/me", cookies={"study_session": cookie}).status_code == 401


@pytest.mark.invariant
def test_production_postgres_disables_bearer_and_enforces_csrf(tmp_path):
    from app.deployment import DeploymentSettings

    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_ENV": "production",
            "STUDY_PLATFORM_DSN": pg_support.app_dsn(),
            # worker 是**另一条凭据边界**：生产自检要求它有独立、非默认的连接串。
            "STUDY_PLATFORM_WORKER_DSN": pg_support.worker_dsn(),
            "STUDY_PLATFORM_SESSION_SECRET": "prod-session-0123456789abcdef0000000",
            "STUDY_PLATFORM_COOKIE_SECRET": "prod-cookie-0123456789abcdef00000000",
            "STUDY_PLATFORM_TOKEN_SECRET": "prod-token-0123456789abcdef0000000000",
            "STUDY_PLATFORM_COOKIE_SECURE": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
            "STUDY_PLATFORM_EXCHANGE_LIMIT": "100",
        }
    )
    platform = build_platform(var_dir=tmp_path, settings=settings)
    assert platform.bearer_enabled is False
    client = TestClient(create_app(platform=platform))

    # bearer 通道关闭：显式 Authorization 头不再是入口。
    bearer_token = platform.sessions.issue(
        principal_id=PRINCIPAL,
        tenant_id=TENANT,
        issued_at=platform.clock.now(),
    )
    denied = client.get(
        "/me",
        headers={"Authorization": f"Bearer {platform.sessions.serialize(bearer_token)}"},
    )
    assert denied.status_code == 401

    # cookie 路径仍可用（显式带 cookie，绕开测试客户端对 Secure 属性的限制）。
    token = "prod-token-" + uuid.uuid4().hex
    _issue_invitation(platform, token)
    exchanged = client.post("/auth/invitations/exchange", json={"token": token})
    assert exchanged.status_code == 200
    cookie = exchanged.cookies["study_session"]
    assert client.get("/me", cookies={"study_session": cookie}).status_code == 200

    # 严格 CSRF：无 Origin/Referer 的不安全请求拒绝。
    no_origin = client.post("/auth/logout", cookies={"study_session": cookie})
    assert no_origin.status_code == 403
    assert no_origin.json()["code"] == "CSRF_DENIED"
    # 白名单外部 Origin 放行。
    ok = client.post(
        "/auth/logout",
        cookies={"study_session": cookie},
        headers={"Origin": "https://app.example.com"},
    )
    assert ok.status_code == 200
