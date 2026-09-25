"""第 1 轮安全收口的 API/单元测试（内存适配器形态）。

覆盖审查报告的 P1/P2 修复在**进程内**可验证的部分：

- 严格 CSRF：无 Origin 的不安全 cookie 请求必须拒绝；Referer 回退；
  可信 Origin 白名单；反代转发头只有显式声明才采信；
- Bearer 通道可整体关闭；
- 注册/登录限流（429 + Retry-After）；
- 认证关键事件全部进入审计事实源；
- cookie 签名密钥轮换（旧 cookie 在轮换窗口内仍可用）；
- 会话集中失效（logout/all）；
- 过期 cookie 声明拒绝；
- 部署配置 fail-fast 聚合。

PostgreSQL 侧（TTL 约束、definer 限流函数、重启恢复、启动自检）见
`test_auth_hardening_postgres.py`。

所有身份一律通过**真实注册 / 登录**取得（`register_user` / `primary_account`
夹具），不再有仓储直发会话或自签 Bearer 的旁路。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.core.clock import FixedClock
from app.core.errors import ErrorCode, PlatformError
from app.deployment import DeploymentSettings
from app.identity.cookie_auth import SESSION_COOKIE_NAME, CookieAuth
from app.identity.memory_store import InMemorySessionRepository
from app.identity.models import Principal
from app.main import build_platform, create_app
from app.product.models import UserSession
from fastapi.testclient import TestClient

SAME_ORIGIN = "http://testserver"
REGISTER_LIMIT = 5


def _client(platform) -> TestClient:
    return TestClient(create_app(platform=platform))


def _register_attempt(platform, *, xff: str | None = None):
    """一个全新客户端发起一次注册尝试（唯一用户名，避免撞已存在的账号）。

    每次用**新客户端**是为了绕开 `already_authenticated`：同一个 cookie jar
    已有会话时注册会被 409 拒掉，测不到限流。TCP 对端都是 "testclient"，
    因此除非可信代理配置命中，限流桶仍是同一个。
    """
    headers = {"Origin": SAME_ORIGIN}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return _client(platform).post(
        "/auth/register",
        json={"username": "u" + uuid.uuid4().hex[:10], "password": "test-pass-1"},
        headers=headers,
    )


# --------------------------------------------------------------- 严格 CSRF


@pytest.mark.invariant
def test_unsafe_cookie_request_without_origin_is_denied(platform, register_user):
    """审查实测：无 Origin 的 /auth/logout 曾返回 200 —— 必须转成拒绝。"""
    _, account = register_user()
    denied = _client(platform).post("/auth/logout", headers={"Cookie": account.headers["Cookie"]})
    assert denied.status_code == 403
    assert denied.json()["code"] == "CSRF_DENIED"


@pytest.mark.invariant
def test_referer_fallback_same_origin_accepted_cross_origin_denied(platform, register_user):
    """没有 Origin 时，同源 Referer 可接受；跨源 Referer 拒绝。"""
    _, first = register_user()
    cookie = {"Cookie": first.headers["Cookie"]}
    ok = _client(platform).post("/auth/logout", headers={**cookie, "Referer": f"{SAME_ORIGIN}/some/page"})
    assert ok.status_code == 200

    _, second = register_user()
    evil = _client(platform).post(
        "/auth/logout",
        headers={"Cookie": second.headers["Cookie"], "Referer": "https://evil.example/phishing"},
    )
    assert evil.status_code == 403
    assert evil.json()["code"] == "CSRF_DENIED"


@pytest.mark.invariant
def test_trusted_origin_allowlist_accepted(tmp_path, register_user):
    """配置的外部 Origin（反代后的外部域名）必须命中白名单。"""
    settings = DeploymentSettings.load({"STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com"})
    platform = build_platform(var_dir=tmp_path, settings=settings)
    _, account = register_user(on=platform)
    ok = _client(platform).post(
        "/auth/logout",
        headers={"Cookie": account.headers["Cookie"], "Origin": "https://app.example.com"},
    )
    assert ok.status_code == 200


@pytest.mark.invariant
def test_forwarded_headers_ignored_unless_behind_proxy(tmp_path, register_user):
    """未声明反代时，X-Forwarded-* 不得参与 Origin 计算（客户端可伪造）。"""
    # 默认配置（behind_proxy=False）：Host 仍是 testserver，
    # 攻击者用转发头把自己伪装成同源，必须不认。
    platform = build_platform(var_dir=tmp_path)
    _, account = register_user(on=platform)
    evil = _client(platform).post(
        "/auth/logout",
        headers={
            "Cookie": account.headers["Cookie"],
            "Host": "testserver",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
        },
    )
    assert evil.status_code == 403

    # 显式声明反代 + 可信代理链后，转发头参与计算，外部来源被接受。
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_BEHIND_PROXY": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://external.example",
            "STUDY_PLATFORM_TRUSTED_PROXIES": "testclient,10.0.0.0/8",
        }
    )
    platform2 = build_platform(var_dir=tmp_path / "p2", settings=settings)
    _, account2 = register_user(on=platform2)
    ok = _client(platform2).post(
        "/auth/logout",
        headers={
            "Cookie": account2.headers["Cookie"],
            "X-Forwarded-Host": "external.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://external.example",
        },
    )
    assert ok.status_code == 200


@pytest.mark.invariant
def test_behind_proxy_without_trusted_proxies_is_rejected():
    """审查 P1 回归：behind_proxy=1 而不配可信代理清单 = 拒绝启动。

    没有可信清单时 XFF 完全由客户端可控（限流键、转发头都可轮换伪造）。
    """
    problems = DeploymentSettings.load({"STUDY_PLATFORM_BEHIND_PROXY": "1"}).configuration_problems()
    assert any("TRUSTED_PROXIES" in p for p in problems)


@pytest.mark.invariant
def test_xff_rotation_cannot_reset_rate_limit_bucket(tmp_path):
    """审查 P1 回归实测复现：轮换 X-Forwarded-For 首值不得重置限流桶。

    请求不来自可信代理（TestClient 对端 "testclient" 不在清单里）时，
    XFF 完全不采信 —— 否则攻击者换一个值就得一个新的限流桶。
    """
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_BEHIND_PROXY": "1",
            # 可信代理只配了一个内网地址 —— testclient 不在其中。
            "STUDY_PLATFORM_TRUSTED_PROXIES": "10.9.9.9",
        }
    )
    platform = build_platform(var_dir=tmp_path, settings=settings)

    statuses = [
        _register_attempt(platform, xff=xff).status_code
        for xff in ["1.2.3.4", "5.6.7.8", "9.9.9.9", "4.3.2.1", "8.7.6.5", "1.1.1.1"]
    ]
    # 六次请求同属一个桶（对端不可信 → 键恒为 TCP 对端）：第 6 次 429。
    assert statuses[:REGISTER_LIMIT] == [201] * REGISTER_LIMIT, statuses
    assert statuses[REGISTER_LIMIT] == 429, f"轮换 XFF 不得重置限流桶：{statuses}"


@pytest.mark.invariant
def test_xff_is_honored_from_trusted_proxy(tmp_path):
    """可信代理转发的 XFF 参与限流：不同客户端 IP 各自计数。"""
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_BEHIND_PROXY": "1",
            "STUDY_PLATFORM_TRUSTED_PROXIES": "testclient",
        }
    )
    platform = build_platform(var_dir=tmp_path, settings=settings)

    # 经可信代理转发的两个不同客户端：各自一个桶，互不影响。
    assert [_register_attempt(platform, xff="203.0.113.7").status_code for _ in range(REGISTER_LIMIT)] == [
        201
    ] * REGISTER_LIMIT
    assert [_register_attempt(platform, xff="203.0.113.8").status_code for _ in range(REGISTER_LIMIT)] == [
        201
    ] * REGISTER_LIMIT
    # 同一客户端再过一次：该桶已满 → 429。
    assert _register_attempt(platform, xff="203.0.113.7").status_code == 429


@pytest.mark.invariant
def test_business_endpoint_csrf_denied_for_cross_origin_cookie(platform, register_user):
    """cookie 认证的业务写端点同样受严格 CSRF 保护（计划补齐项）。"""
    _, account = register_user()
    response = _client(platform).post(
        f"/projects/{account.project_id}/interactions",
        headers={"Cookie": account.headers["Cookie"], "Origin": "https://evil.example"},
        json={"node_id": "diagnose", "user_input": "hi"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "CSRF_DENIED"


# ------------------------------------------------------- Bearer 开关


@pytest.mark.invariant
def test_bearer_channel_can_be_disabled(platform, register_user):
    """生产形态关闭 bearer：显式 Authorization 头不再是入口，cookie 仍可用。"""
    platform.bearer_enabled = False
    client = _client(platform)

    token = platform.sessions.issue(
        principal_id="user_someone",
        tenant_id="tenant_someone",
        issued_at=platform.clock.now(),
    )
    bearer = client.get(
        "/me",
        headers={"Authorization": f"Bearer {platform.sessions.serialize(token)}"},
    )
    assert bearer.status_code == 401

    # cookie 路径不受影响（真实注册得到的会话）。
    _, account = register_user()
    assert client.get("/me", headers={"Cookie": account.headers["Cookie"]}).status_code == 200


# ------------------------------------------------------- 限流


@pytest.mark.invariant
def test_register_rate_limit_returns_429_with_retry_after(tmp_path):
    platform = build_platform(var_dir=tmp_path)
    statuses = [_register_attempt(platform).status_code for _ in range(REGISTER_LIMIT + 1)]
    assert statuses[:REGISTER_LIMIT] == [201] * REGISTER_LIMIT
    assert statuses[REGISTER_LIMIT] == 429

    blocked = _register_attempt(platform)
    assert blocked.json()["retryable"] is True
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None
    assert 0 < int(retry_after) <= 3600


@pytest.mark.invariant
def test_rate_limiter_windows_align_between_memory_and_formula():
    """内存窗口对齐公式必须与 PG 的 epoch 整除一致（跨适配器语义）。"""
    from app.identity.rate_limit import InMemoryRateLimiter, window_start_epoch

    now = datetime(2026, 9, 19, 12, 34, 56, tzinfo=timezone.utc)
    assert window_start_epoch(now, 600) == (int(now.timestamp()) // 600) * 600

    limiter = InMemoryRateLimiter(limit=2, window_seconds=60)
    d1 = limiter.register("k", now=now)
    d2 = limiter.register("k", now=now)
    d3 = limiter.register("k", now=now)
    assert (d1.allowed, d2.allowed, d3.allowed) == (True, True, False)
    assert d3.attempts == 3
    # 窗口滚动后重新计数。
    later = now + timedelta(seconds=61)
    d4 = limiter.register("k", now=later)
    assert d4.allowed is True and d4.attempts == 1
    # 不同客户端键互不影响。
    assert limiter.register("other", now=now).allowed is True


# ------------------------------------------------------- 审计事件


def _event_types(platform) -> list[str]:
    return [record.get("event_type") for record in platform.audit.read_all()]


def _records(platform, event_type: str) -> list[dict]:
    return [record for record in platform.audit.read_all() if record.get("event_type") == event_type]


@pytest.mark.invariant
def test_registration_audit_redacts_session_material(platform, register_user):
    """注册审计可关联主体，但不得复制会话标识。"""
    _, account = register_user()
    records = _records(platform, "account_registered")
    assert len(records) == 1
    assert "session_id" not in records[0]["payload"]
    assert records[0]["payload"]["principal_id"] == account.principal_id


@pytest.mark.invariant
def test_authentication_critical_events_are_audited(platform, register_user):
    client = _client(platform)
    _, account = register_user()
    cookie = {"Cookie": account.headers["Cookie"]}
    # 认证失败（无凭据）
    _client(platform).get("/me")
    # 退出
    client.post("/auth/logout", headers={**cookie, "Origin": SAME_ORIGIN})

    types = _event_types(platform)
    assert "account_registered" in types
    assert "authentication_failed" in types
    assert "session_revoked" in types


@pytest.mark.invariant
def test_rate_limit_event_is_audited(tmp_path):
    platform = build_platform(var_dir=tmp_path)
    for _ in range(REGISTER_LIMIT + 1):
        _register_attempt(platform)
    assert "auth_rate_limited" in _event_types(platform)


# ------------------------------------------------------- 密钥轮换


@pytest.mark.invariant
def test_cookie_key_rotation_keeps_old_cookie_until_expiry(tmp_path, register_user):
    old_secret = "old-cookie-secret-0123456789abcdef"
    new_secret = "new-cookie-secret-0123456789abcdef0"

    # 平台轮换到新密钥，旧密钥放进 previous_secrets。
    platform = build_platform(
        var_dir=tmp_path,
        settings=DeploymentSettings.load(
            {
                "STUDY_PLATFORM_COOKIE_SECRET": new_secret,
                "STUDY_PLATFORM_COOKIE_SECRET_PREVIOUS": old_secret,
            }
        ),
    )
    # 与平台共享同一时钟，避免固定时钟与系统时钟错位导致会话过期判定漂移。
    _, account = register_user(on=platform)
    claims = platform.cookie_auth.verify(account.cookie, now=platform.clock.now())
    session = platform.session_store.get_live(claims.to_principal(), claims.session_id)
    assert session is not None, "真实注册建立的会话必须可回读"

    # 用旧密钥签发的 cookie（模拟轮换前已登录的用户）。
    old_auth = CookieAuth(old_secret, platform.clock)
    old_cookie = old_auth.issue(session)
    client = _client(platform)

    # 旧 cookie 仍可通过验签 + 回库。
    assert client.get("/me", cookies={SESSION_COOKIE_NAME: old_cookie}).status_code == 200
    # 新签发的 cookie 只由新密钥签名：旧密钥验签直接拒绝，新密钥验得过。
    with pytest.raises(PlatformError) as excinfo:
        old_auth.verify(account.cookie, now=platform.clock.now())
    assert excinfo.value.code is ErrorCode.AUTH_REQUIRED
    assert platform.cookie_auth.verify(account.cookie, now=platform.clock.now()) is not None


# ------------------------------------------------------- 集中失效


@pytest.mark.invariant
def test_logout_all_revokes_every_session_of_principal(platform, register_user):
    session_a, account = register_user()
    # 同一账号第二次登录（新客户端，避免 already_authenticated）→ 第二个会话。
    login_client = _client(platform)
    logged_in = login_client.post(
        "/auth/login",
        json={"username": account.username, "password": account.password},
        headers={"Origin": SAME_ORIGIN},
    )
    assert logged_in.status_code == 200
    cookie_b = login_client.cookies.get(SESSION_COOKIE_NAME)

    cookie_a = account.headers["Cookie"]
    assert session_a.get("/me", headers={"Cookie": cookie_a}).status_code == 200
    assert login_client.get("/me", headers={"Cookie": f"study_session={cookie_b}"}).status_code == 200

    response = session_a.post("/auth/logout/all", headers={"Cookie": cookie_a, "Origin": SAME_ORIGIN})
    assert response.status_code == 200
    assert response.json()["revoked"] >= 2

    # 两份 cookie 都立即失效。
    assert session_a.get("/me", headers={"Cookie": cookie_a}).status_code == 401
    assert login_client.get("/me", headers={"Cookie": f"study_session={cookie_b}"}).status_code == 401
    assert "sessions_revoked_all" in _event_types(platform)


@pytest.mark.invariant
def test_revoke_all_for_contract_memory():
    clock = FixedClock(datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))
    store = InMemorySessionRepository(clock=clock)
    actor = Principal(principal_id="u1", tenant_id="t1")
    other = Principal(principal_id="u2", tenant_id="t1")
    now = clock.now()

    def _session(sid: str, principal_id: str) -> UserSession:
        return UserSession(
            session_id=sid,
            tenant_id="t1",
            principal_id=principal_id,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            credential_id="cred_" + principal_id,
            security_generation=1,
        )

    for sid in ("s1", "s2", "s3"):
        store.create(_session(sid, "u1"))
    store.create(_session("s4", "u2"))
    # 保留当前会话 s1，撤掉其余两台设备。
    assert store.revoke_all_for(actor, at=now, except_session_id="s1") == 2
    assert store.get_live(actor, "s1") is not None
    assert store.get_live(actor, "s2") is None
    assert store.get_live(actor, "s3") is None
    # 别人的会话仍然存活（归属隔离：u1 撤不到 u2）。
    assert store.get_live(other, "s4") is not None
    # 不带例外再撤一次：这次轮到 s1。
    assert store.revoke_all_for(actor, at=now) == 1
    # 幂等：再撤没有存活会话可撤。
    assert store.revoke_all_for(actor, at=now) == 0
    # u2 自己可以撤自己的会话。
    assert store.revoke_all_for(other, at=now) == 1


# ------------------------------------------------------- 过期 cookie


@pytest.mark.invariant
def test_expired_cookie_claims_rejected(platform, register_user):
    _, account = register_user()
    now = platform.clock.now()
    expired = UserSession(
        session_id="sess_expired",
        tenant_id=account.tenant_id,
        principal_id=account.principal_id,
        issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
        credential_id="cred_expired",
        security_generation=1,
    )
    cookie = platform.cookie_auth.issue(expired)
    with pytest.raises(PlatformError) as excinfo:
        platform.cookie_auth.verify(cookie, now=now)
    assert excinfo.value.code is ErrorCode.AUTH_REQUIRED

    response = _client(platform).get("/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert response.status_code == 401


# ------------------------------------------------------- 部署配置自检


@pytest.mark.invariant
def test_production_settings_fail_fast_lists_all_problems():
    problems = DeploymentSettings.load({"STUDY_PLATFORM_ENV": "production"}).configuration_problems()
    joined = "\n".join(problems)
    assert "STUDY_PLATFORM_DSN" in joined
    assert "STUDY_PLATFORM_SESSION_SECRET" in joined
    assert "STUDY_PLATFORM_COOKIE_SECRET" in joined
    assert "STUDY_PLATFORM_TOKEN_SECRET" in joined
    assert "COOKIE_SECURE" in joined
    assert "TRUSTED_ORIGINS" in joined
    # 一次性报全部，不挤牙膏。
    assert len(problems) >= 6


@pytest.mark.invariant
def test_production_rejects_cookie_secret_equal_session_secret():
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_ENV": "production",
            # 占位 DSN：带上 password 标记（dev-only）以免秘密扫描误报，仅测配置解析。
            "STUDY_PLATFORM_DSN": "postgresql://study_app:dev-only-change-me@h/db",
            "STUDY_PLATFORM_SESSION_SECRET": "same-secret-0123456789abcdef0000",
            "STUDY_PLATFORM_COOKIE_SECRET": "same-secret-0123456789abcdef0000",
            "STUDY_PLATFORM_TOKEN_SECRET": "token-secret-0123456789abcdef00000",
            "STUDY_PLATFORM_COOKIE_SECURE": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
        }
    )
    assert any("分离" in p for p in settings.configuration_problems())


@pytest.mark.invariant
def test_production_rejects_short_secrets():
    """审查 P1 回归：一字节的生产密钥必须被拒绝。

    审查实测曾通过：session="a", cookie="b", token="b" —— 一字节 HMAC
    密钥可被在线穷举。
    """
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_ENV": "production",
            "STUDY_PLATFORM_DSN": "postgresql://study_app:dev-only-change-me@h/db",
            "STUDY_PLATFORM_SESSION_SECRET": "a",
            "STUDY_PLATFORM_COOKIE_SECRET": "b",
            "STUDY_PLATFORM_TOKEN_SECRET": "b",
            "STUDY_PLATFORM_COOKIE_SECURE": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
            "STUDY_PLATFORM_TRUSTED_PROXIES": "10.0.0.1",
        }
    )
    problems = settings.configuration_problems()
    joined = "\n".join(problems)
    assert "SESSION_SECRET" in joined and "32" in joined, "短密钥必须被点名"
    # token 与 cookie 同为 "b"：交叉复用必须被点名（不止 cookie/session 一对）。
    assert any("TOKEN" in p and "重复" in p for p in problems)


@pytest.mark.invariant
def test_production_rejects_cross_reuse_with_previous_cookie_secret():
    """历史轮换密钥也参与交叉复用检查：新密钥不得复用任何在用/历史密钥。"""
    base = {
        "STUDY_PLATFORM_ENV": "production",
        "STUDY_PLATFORM_DSN": "postgresql://study_app:dev-only-change-me@h/db",
        "STUDY_PLATFORM_SESSION_SECRET": "session-secret-0123456789abcdef00",
        "STUDY_PLATFORM_COOKIE_SECRET": "cookie-secret-0123456789abcdef0000",
        "STUDY_PLATFORM_TOKEN_SECRET": "token-secret-0123456789abcdef00000",
        "STUDY_PLATFORM_COOKIE_SECRET_PREVIOUS": "session-secret-0123456789abcdef00",
        "STUDY_PLATFORM_COOKIE_SECURE": "1",
        "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
        "STUDY_PLATFORM_TRUSTED_PROXIES": "10.0.0.1",
    }
    problems = DeploymentSettings.load(base).configuration_problems()
    assert any("COOKIE_PREVIOUS" in p and "重复" in p for p in problems)


@pytest.mark.invariant
def test_production_accepts_strong_distinct_secrets():
    """正面用例：足够长且两两互异的密钥通过自检（其余生产项已满足）。"""
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_ENV": "production",
            "STUDY_PLATFORM_DSN": "postgresql://study_app:dev-only-change-me@h/db",
            "STUDY_PLATFORM_WORKER_DSN": "postgresql://study_worker:dev-only-change-me@h/db",
            "STUDY_PLATFORM_SESSION_SECRET": "session-secret-0123456789abcdef00",
            "STUDY_PLATFORM_COOKIE_SECRET": "cookie-secret-0123456789abcdef0000",
            "STUDY_PLATFORM_TOKEN_SECRET": "token-secret-0123456789abcdef00000",
            "STUDY_PLATFORM_COOKIE_SECURE": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
            "STUDY_PLATFORM_TRUSTED_PROXIES": "10.0.0.1",
        }
    )
    assert settings.configuration_problems() == []


@pytest.mark.invariant
def test_production_requires_a_separate_worker_credential():
    """worker 与 API 必须是**两条凭据边界**（0008：队列的跨租户策略只授予
    `study_worker`）。

    三种坏配置各自被点名：没配、配成本机默认值（无密码的本地值）、
    与应用程序串**相同**（那就等于没有分开 —— 而应用角色已经拿不到
    `UPDATE ingestion_jobs`，worker 用它会一条任务也认领不到）。
    """
    base = {
        "STUDY_PLATFORM_ENV": "production",
        "STUDY_PLATFORM_DSN": "postgresql://study_app:dev-only-change-me@h/db",
        "STUDY_PLATFORM_SESSION_SECRET": "session-secret-0123456789abcdef00",
        "STUDY_PLATFORM_COOKIE_SECRET": "cookie-secret-0123456789abcdef0000",
        "STUDY_PLATFORM_TOKEN_SECRET": "token-secret-0123456789abcdef00000",
        "STUDY_PLATFORM_COOKIE_SECURE": "1",
        "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com",
        "STUDY_PLATFORM_TRUSTED_PROXIES": "10.0.0.1",
    }

    missing = DeploymentSettings.load(base).configuration_problems()
    assert any("WORKER_DSN" in p for p in missing), missing

    from app.db.settings import DEFAULT_WORKER_DSN

    defaulted = DeploymentSettings.load(
        {**base, "STUDY_PLATFORM_WORKER_DSN": DEFAULT_WORKER_DSN}
    ).configuration_problems()
    assert any("WORKER_DSN" in p and "默认值" in p for p in defaulted), defaulted

    same = DeploymentSettings.load(
        {**base, "STUDY_PLATFORM_WORKER_DSN": base["STUDY_PLATFORM_DSN"]}
    ).configuration_problems()
    assert any("WORKER_DSN" in p and "相同" in p for p in same), same


@pytest.mark.invariant
def test_unknown_mode_and_bad_origin_are_rejected():
    with pytest.raises(RuntimeError):
        DeploymentSettings.load({"STUDY_PLATFORM_ENV": "prod"})
    settings = DeploymentSettings.load({"STUDY_PLATFORM_TRUSTED_ORIGINS": "not-a-url"})
    assert any("Origin" in p for p in settings.configuration_problems())


def test_source_search_provider_requires_key_and_keeps_it_out_of_repr():
    settings = DeploymentSettings.load({"STUDY_PLATFORM_SOURCE_SEARCH_PROVIDER": "tavily"})
    assert any("SOURCE_SEARCH_API_KEY" in problem for problem in settings.configuration_problems())

    configured = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_SOURCE_SEARCH_PROVIDER": "tavily",
            "STUDY_PLATFORM_SOURCE_SEARCH_API_KEY": "private-search-key",
        }
    )
    assert not configured.configuration_problems()
    assert "private-search-key" not in repr(configured)

    with pytest.raises(RuntimeError, match="SOURCE_SEARCH_PROVIDER"):
        DeploymentSettings.load({"STUDY_PLATFORM_SOURCE_SEARCH_PROVIDER": "tavilly"})


def test_optional_local_query_rewriter_requires_literal_loopback_and_short_timeout():
    disabled = DeploymentSettings.load({})
    assert not any("本地查询改写" in problem for problem in disabled.configuration_problems())

    configured = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_URL": ("http://127.0.0.1:11434/v1/chat/completions"),
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_MODEL": "qwen-local",
        }
    )
    assert not any("本地查询改写" in problem for problem in configured.configuration_problems())

    remote = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_URL": ("http://192.168.1.10:11434/v1/chat/completions"),
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_MODEL": "qwen-local",
        }
    )
    assert any("loopback" in problem for problem in remote.configuration_problems())

    unbounded = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_URL": ("http://127.0.0.1:11434/v1/chat/completions"),
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_MODEL": "qwen-local",
            "STUDY_PLATFORM_LOCAL_QUERY_REWRITER_TIMEOUT_SECONDS": "4",
        }
    )
    assert any("超时" in problem for problem in unbounded.configuration_problems())


@pytest.mark.invariant
def test_ttl_over_hard_cap_rejected_at_configuration():
    settings = DeploymentSettings.load({"STUDY_PLATFORM_SESSION_TTL_MINUTES": str(60 * 24 * 31)})
    assert any("TTL" in p for p in settings.configuration_problems())


@pytest.mark.invariant
def test_auth_attempt_limit_must_be_positive():
    settings = DeploymentSettings.load({"STUDY_PLATFORM_AUTH_ATTEMPT_LIMIT": "0"})
    assert any("AUTH_ATTEMPT_LIMIT" in p for p in settings.configuration_problems())
