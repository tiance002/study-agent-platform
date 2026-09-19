"""第 1 轮安全收口的 API/单元测试（内存适配器形态）。

覆盖审查报告的 P1/P2 修复在**进程内**可验证的部分：

- 严格 CSRF：无 Origin 的不安全 cookie 请求必须拒绝；Referer 回退；
  可信 Origin 白名单；反代转发头只有显式声明才采信；
- Bearer 通道可整体关闭；
- 邀请兑换限流（429 + Retry-After）；
- 认证关键事件全部进入审计事实源；
- cookie 签名密钥轮换（旧 cookie 在轮换窗口内仍可用）；
- 会话集中失效（logout/all）；
- 过期 cookie 声明拒绝；
- 内存兑换原子性（建会话失败回滚消费标记）与并发单次消费；
- 会话 TTL 硬上限；
- 部署配置 fail-fast 聚合。

PostgreSQL 侧（TTL 约束、definer 限流函数、重启恢复、启动自检）见
`test_auth_hardening_postgres.py`。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from app.core.clock import FixedClock
from app.core.errors import ErrorCode, PlatformError
from app.deployment import DeploymentSettings
from app.identity.cookie_auth import SESSION_COOKIE_NAME, CookieAuth
from app.identity.memory_store import (
    InMemoryInvitationRepository,
    InMemorySessionRepository,
)
from app.identity.models import Principal
from app.identity.ports import SystemContext
from app.main import DEMO_PRINCIPAL, DEMO_TENANT, build_platform, create_app
from app.product.models import UserSession
from fastapi.testclient import TestClient

SAME_ORIGIN = "http://testserver"


def _hash(raw: str) -> str:
    return f"sha256:{sha256(raw.encode()).hexdigest()}"


def _issue(platform, token: str) -> None:
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "测试邀请"),
        invitation_id="inv_" + uuid.uuid4().hex,
        token_hash=_hash(token),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )


def _client(platform) -> TestClient:
    return TestClient(create_app(platform=platform))


@pytest.fixture
def ready(platform):
    """带一条有效邀请的平台。"""
    _issue(platform, "token-live")
    return platform


# --------------------------------------------------------------- 严格 CSRF


@pytest.mark.invariant
def test_unsafe_cookie_request_without_origin_is_denied(ready):
    """审查实测：无 Origin 的 /auth/logout 曾返回 200 —— 必须转成拒绝。"""
    client = _client(ready)
    assert client.post("/auth/invitations/exchange", json={"token": "token-live"}).status_code == 200

    denied = client.post("/auth/logout")
    assert denied.status_code == 403
    assert denied.json()["code"] == "CSRF_DENIED"


@pytest.mark.invariant
def test_referer_fallback_same_origin_accepted_cross_origin_denied(ready):
    """没有 Origin 时，同源 Referer 可接受；跨源 Referer 拒绝。"""
    client = _client(ready)
    client.post("/auth/invitations/exchange", json={"token": "token-live"})

    ok = client.post("/auth/logout", headers={"Referer": f"{SAME_ORIGIN}/some/page"})
    assert ok.status_code == 200

    # 第二个会话用于恶意 Referer 探测（上个会话已退出）。
    _issue(ready, "token-two")
    client.post("/auth/invitations/exchange", json={"token": "token-two"})
    evil = client.post(
        "/auth/logout",
        headers={"Referer": "https://evil.example/phishing"},
    )
    assert evil.status_code == 403
    assert evil.json()["code"] == "CSRF_DENIED"


@pytest.mark.invariant
def test_trusted_origin_allowlist_accepted(tmp_path):
    """配置的外部 Origin（反代后的外部域名）必须命中白名单。"""
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_TRUSTED_ORIGINS": "https://app.example.com"}
    )
    platform = build_platform(var_dir=tmp_path, settings=settings)
    _issue(platform, "token-x")
    client = _client(platform)
    client.post("/auth/invitations/exchange", json={"token": "token-x"})

    ok = client.post(
        "/auth/logout",
        headers={"Origin": "https://app.example.com"},
    )
    assert ok.status_code == 200


@pytest.mark.invariant
def test_forwarded_headers_ignored_unless_behind_proxy(tmp_path):
    """未声明反代时，X-Forwarded-* 不得参与 Origin 计算（客户端可伪造）。"""
    # 默认配置（behind_proxy=False）：Host 仍是 testserver，
    # 攻击者用转发头把自己伪装成同源，必须不认。
    platform = build_platform(var_dir=tmp_path)
    _issue(platform, "token-fwd")
    client = _client(platform)
    client.post("/auth/invitations/exchange", json={"token": "token-fwd"})
    evil = client.post(
        "/auth/logout",
        headers={
            "Host": "testserver",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
        },
    )
    assert evil.status_code == 403

    # 显式声明反代后，转发头参与计算，外部来源被接受。
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_BEHIND_PROXY": "1",
            "STUDY_PLATFORM_TRUSTED_ORIGINS": "https://external.example",
        }
    )
    platform2 = build_platform(var_dir=tmp_path / "p2", settings=settings)
    _issue(platform2, "token-fwd2")
    client2 = _client(platform2)
    client2.post("/auth/invitations/exchange", json={"token": "token-fwd2"})
    ok = client2.post(
        "/auth/logout",
        headers={
            "X-Forwarded-Host": "external.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://external.example",
        },
    )
    assert ok.status_code == 200


@pytest.mark.invariant
def test_business_endpoint_csrf_denied_for_cross_origin_cookie(ready):
    """cookie 认证的业务写端点同样受严格 CSRF 保护（计划补齐项）。"""
    client = _client(ready)
    client.post("/auth/invitations/exchange", json={"token": "token-live"})
    response = client.post(
        "/projects/proj_demo/interactions",
        headers={"Origin": "https://evil.example"},
        json={"node_id": "diagnose", "user_input": "hi"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "CSRF_DENIED"


# ------------------------------------------------------- Bearer 开关


@pytest.mark.invariant
def test_bearer_channel_can_be_disabled(ready):
    """生产形态关闭 bearer：显式 Authorization 头不再是入口，cookie 仍可用。"""
    ready.bearer_enabled = False
    client = _client(ready)

    token = ready.sessions.issue(
        principal_id=DEMO_PRINCIPAL,
        tenant_id=DEMO_TENANT,
        issued_at=ready.clock.now(),
    )
    bearer = client.get(
        "/me",
        headers={"Authorization": f"Bearer {ready.sessions.serialize(token)}"},
    )
    assert bearer.status_code == 401

    # cookie 路径不受影响。
    _issue(ready, "token-cookie")
    assert (
        client.post("/auth/invitations/exchange", json={"token": "token-cookie"}).status_code
        == 200
    )
    assert client.get("/me").status_code == 200


# ------------------------------------------------------- 限流


@pytest.mark.invariant
def test_exchange_rate_limit_returns_429_with_retry_after(tmp_path):
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_EXCHANGE_LIMIT": "3", "STUDY_PLATFORM_EXCHANGE_WINDOW_SECONDS": "600"}
    )
    platform = build_platform(var_dir=tmp_path, settings=settings)
    client = _client(platform)

    statuses = [
        client.post(
            "/auth/invitations/exchange",
            json={"token": f"any-token-{i}"},
        ).status_code
        for i in range(6)
    ]
    # 前 3 次进入兑换逻辑（401：令牌不存在），第 4 次起被限流（429）。
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429, 429]

    blocked = client.post("/auth/invitations/exchange", json={"token": "any-token-z"})
    assert blocked.json()["retryable"] is True
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None
    assert 0 < int(retry_after) <= 600


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


@pytest.mark.invariant
def test_authentication_critical_events_are_audited(ready):
    client = _client(ready)

    # 兑换成功
    client.post("/auth/invitations/exchange", json={"token": "token-live"})
    # 兑换被拒（未知令牌）
    client.post("/auth/invitations/exchange", json={"token": "no-such-token"})
    # 认证失败（无凭据）—— 用新客户端避免携带已有 cookie
    _client(ready).get("/me")
    # 退出
    client.post("/auth/logout", headers={"Origin": SAME_ORIGIN})

    types = _event_types(ready)
    assert "invitation_exchanged" in types
    assert "invitation_rejected" in types
    assert "authentication_failed" in types
    assert "session_revoked" in types

    # 审计载荷不得包含令牌材料。
    raw = ready.audit.path.read_text(encoding="utf-8")
    assert "token-live" not in raw
    assert _hash("token-live") not in raw


@pytest.mark.invariant
def test_rate_limit_event_is_audited(tmp_path):
    settings = DeploymentSettings.load({"STUDY_PLATFORM_EXCHANGE_LIMIT": "1"})
    platform = build_platform(var_dir=tmp_path, settings=settings)
    client = _client(platform)
    client.post("/auth/invitations/exchange", json={"token": "a"})
    client.post("/auth/invitations/exchange", json={"token": "b"})
    assert "auth_rate_limited" in _event_types(platform)


# ------------------------------------------------------- 密钥轮换


@pytest.mark.invariant
def test_cookie_key_rotation_keeps_old_cookie_until_expiry(tmp_path):
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
    now = platform.clock.now()
    session = UserSession(
        session_id="sess_rotate",
        tenant_id=DEMO_TENANT,
        principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    # 用旧密钥签发的 cookie（模拟轮换前已登录的用户）。
    old_auth = CookieAuth(old_secret, platform.clock)
    old_cookie = old_auth.issue(session)
    platform.session_store.create(session)
    client = _client(platform)

    # 旧 cookie 仍可通过验签 + 回库。
    assert client.get("/me", cookies={SESSION_COOKIE_NAME: old_cookie}).status_code == 200
    # 新兑换出的 cookie 由新密钥签名：旧密钥验不过。
    _issue(platform, "token-rot")
    exchanged = client.post("/auth/invitations/exchange", json={"token": "token-rot"})
    new_cookie = exchanged.cookies[SESSION_COOKIE_NAME]
    # 新签发的 cookie 只由新密钥签名：旧密钥验签直接拒绝，新密钥验得过。
    with pytest.raises(PlatformError) as excinfo:
        old_auth.verify(new_cookie, now=platform.clock.now())
    assert excinfo.value.code is ErrorCode.AUTH_REQUIRED
    assert platform.cookie_auth.verify(new_cookie, now=platform.clock.now()) is not None


# ------------------------------------------------------- 集中失效


@pytest.mark.invariant
def test_logout_all_revokes_every_session_of_principal(ready):
    _issue(ready, "token-a")
    _issue(ready, "token-b")
    client_a = _client(ready)
    client_b = _client(ready)
    client_a.post("/auth/invitations/exchange", json={"token": "token-a"})
    client_b.post("/auth/invitations/exchange", json={"token": "token-b"})
    assert client_a.get("/me").status_code == 200
    assert client_b.get("/me").status_code == 200

    response = client_a.post("/auth/logout/all", headers={"Origin": SAME_ORIGIN})
    assert response.status_code == 200
    assert response.json()["revoked"] >= 2

    # 两份 cookie 都立即失效。
    assert client_a.get("/me").status_code == 401
    assert client_b.get("/me").status_code == 401
    assert "sessions_revoked_all" in _event_types(ready)


@pytest.mark.invariant
def test_revoke_all_for_contract_memory():
    clock = FixedClock(datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))
    store = InMemorySessionRepository(clock=clock)
    actor = Principal(principal_id="u1", tenant_id="t1")
    other = Principal(principal_id="u2", tenant_id="t1")
    now = clock.now()
    for sid in ("s1", "s2", "s3"):
        store.create(
            UserSession(
                session_id=sid,
                tenant_id="t1",
                principal_id="u1",
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    store.create(
        UserSession(
            session_id="s4",
            tenant_id="t1",
            principal_id="u2",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
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
def test_expired_cookie_claims_rejected(ready):
    now = ready.clock.now()
    expired = UserSession(
        session_id="sess_expired",
        tenant_id=DEMO_TENANT,
        principal_id=DEMO_PRINCIPAL,
        issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
    )
    ready.session_store.create(expired)
    cookie = ready.cookie_auth.issue(expired)
    client = _client(ready)
    response = client.get("/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert response.status_code == 401


# ------------------------------------------------------- TTL 与原子性


@pytest.mark.invariant
def test_memory_exchange_rejects_session_beyond_ttl_cap(ready):
    """服务端传入超上限的会话期限：拒绝，且邀请保持可用。"""
    now = ready.clock.now()
    with pytest.raises(Exception) as exc_info:
        ready.invitations.exchange(
            _hash("token-live"),
            session_id="sess_bad",
            session_expires_at=now + timedelta(days=36500),
        )
    assert exc_info.value.code is ErrorCode.INTERNAL_CONSISTENCY_ERROR

    # 邀请没有被消费：随后用合法期限兑换成功。
    session = ready.invitations.exchange(
        _hash("token-live"),
        session_id="sess_ok",
        session_expires_at=now + timedelta(hours=8),
    )
    assert session is not None and session.session_id == "sess_ok"


class _FailingSessionStore(InMemorySessionRepository):
    """第一次 create 必失败，之后恢复正常 —— 模拟会话登记失败。"""

    def __init__(self, clock) -> None:
        super().__init__(clock=clock)
        self.calls = 0

    def create(self, session) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        super().create(session)


@pytest.mark.invariant
def test_memory_exchange_rolls_back_consumption_when_session_create_fails():
    """建会话失败时邀请必须回到未消费状态（P2 原子性）。"""
    clock = FixedClock(datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))
    sessions = _FailingSessionStore(clock)
    invitations = InMemoryInvitationRepository(clock=clock, sessions=sessions)
    now = clock.now()
    invitations.issue(
        SystemContext("t1", "t"),
        invitation_id="inv_1",
        token_hash=_hash("tok"),
        issued_by="u1",
        invitee_principal_id="u1",
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )

    with pytest.raises(RuntimeError, match="boom"):
        invitations.exchange(
            _hash("tok"),
            session_id="sess_first",
            session_expires_at=now + timedelta(hours=8),
        )
    # 第二次兑换成功 —— 证明邀请没有被悬空消费。
    session = invitations.exchange(
        _hash("tok"),
        session_id="sess_second",
        session_expires_at=now + timedelta(hours=8),
    )
    assert session is not None and session.session_id == "sess_second"


@pytest.mark.invariant
def test_memory_concurrent_exchange_consumes_once(ready, racy_scheduling):
    """同一邀请并发兑换：恰好一次成功（与 PG 行锁语义对齐）。"""
    barrier = threading.Barrier(8)
    results: list = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            session = ready.invitations.exchange(
                _hash("token-live"),
                session_id=f"sess_conc_{i}",
                session_expires_at=ready.clock.now() + timedelta(hours=8),
            )
            results.append(session)
        except BaseException as exc:  # noqa: BLE001 - 并发测试要收集一切异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    successes = [s for s in results if s is not None]
    assert len(successes) == 1, f"并发兑换必须只成功一次，实际 {len(successes)}"


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
def test_unknown_mode_and_bad_origin_are_rejected():
    with pytest.raises(RuntimeError):
        DeploymentSettings.load({"STUDY_PLATFORM_ENV": "prod"})
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_TRUSTED_ORIGINS": "not-a-url"}
    )
    assert any("Origin" in p for p in settings.configuration_problems())


@pytest.mark.invariant
def test_ttl_over_hard_cap_rejected_at_configuration():
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_SESSION_TTL_MINUTES": str(60 * 24 * 31)}
    )
    assert any("TTL" in p for p in settings.configuration_problems())
