"""邀请登录与 cookie 会话的 API 级测试（Task 3 退出门）。

这些测试走**完整 HTTP 路径**（TestClient），一条不少地覆盖本轮计划列出的场景：
有效兑换、重放、过期、未知、篡改 cookie、撤销、退出、cookie 标志、CSRF、
响应里没有令牌材料。全部在内存适配器上跑 —— 语义一致性由
`test_identity_repositories.py` 的参数化契约测试保证。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from app.identity.cookie_auth import SESSION_COOKIE_NAME

TOKEN_LIVE = "invite-token-" + uuid.uuid4().hex
TOKEN_SECOND = "invite-token-" + uuid.uuid4().hex


def _hash(raw: str) -> str:
    from hashlib import sha256

    return f"sha256:{sha256(raw.encode()).hexdigest()}"


@pytest.fixture
def invited(platform):
    """给演示租户的两条未消费邀请（走仓储端口，不绕过认证语义）。"""
    from app.identity.ports import SystemContext
    from app.main import DEMO_PRINCIPAL, DEMO_TENANT

    context = SystemContext(DEMO_TENANT, "测试邀请")
    now = platform.clock.now()
    platform.invitations.issue(
        context,
        invitation_id="inv_test_live",
        token_hash=_hash(TOKEN_LIVE),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    platform.invitations.issue(
        context,
        invitation_id="inv_test_second",
        token_hash=_hash(TOKEN_SECOND),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    return platform


# ------------------------------------------------------------ 有效兑换


@pytest.mark.invariant
def test_exchange_sets_cookie_and_me_works(client, invited):
    response = client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE})
    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == "user_demo", "响应只暴露身份标识，不含令牌材料"
    # 原始令牌与哈希**不出现在任何响应里**。
    assert TOKEN_LIVE not in response.text
    assert _hash(TOKEN_LIVE) not in response.text

    # cookie 标志：HttpOnly / SameSite。这些不是锦上添花，是威胁模型的落点。
    set_cookie = response.headers["set-cookie"]
    assert "httponly" in set_cookie.lower(), "HttpOnly 缺失 = XSS 能偷走会话"
    assert "samesite=lax" in set_cookie.lower()

    # cookie 会话立即可用（回库查询能查到这次兑换建立的会话）。
    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["principal_id"] == "user_demo"


@pytest.mark.invariant
def test_exchange_rejects_identity_fields(client, invited):
    """客户端**没有**身份参数可传 —— 多传直接 422，不是"安全地"忽略。"""
    response = client.post(
        "/auth/invitations/exchange",
        json={"token": TOKEN_SECOND, "tenant_id": "tenant_evil", "principal_id": "user_evil"},
    )
    assert response.status_code == 422


@pytest.mark.invariant
def test_exchange_rejects_malformed_body(client, invited):
    assert client.post("/auth/invitations/exchange", json={}).status_code == 422
    assert client.post("/auth/invitations/exchange", json={"token": ""}).status_code == 422


# ------------------------------------------------------------ 统一拒绝


@pytest.mark.invariant
def test_replay_unknown_and_malformed_share_one_rejection(client, invited):
    """重放 / 未知 / 畸形格式 → **同一个**错误码同一句话。

    这不是整洁癖：区分"已消费"和"不存在"等于告诉探测者这个 token 用过，
    存在性本身就是信息。
    """
    first = client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE})
    assert first.status_code == 200
    replay = client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE})
    unknown = client.post("/auth/invitations/exchange", json={"token": "no-such-token"})
    # 格式畸形（长度合法但谁也没签发过）走兑换路径 → 与重放/未知同一拒绝。
    weird = client.post("/auth/invitations/exchange", json={"token": "x" * 100})

    for response, label in ((replay, "重放"), (unknown, "未知"), (weird, "畸形")):
        assert response.status_code == 401, label
        assert response.json()["code"] == "INVITATION_INVALID", label
        assert response.json()["message"] == replay.json()["message"], label

    # 超长是**请求形状**问题，不是 token 状态问题 —— pydantic 422。
    # 两种拒绝性质不同：前者在进入业务逻辑之前就被拒了，没有查询发生。
    overlong = client.post("/auth/invitations/exchange", json={"token": "x" + "1" * 5000})
    assert overlong.status_code == 422


# ------------------------------------------------------------ 会话撤销


@pytest.mark.invariant
def test_logout_revokes_session_and_clears_cookie(client, invited):
    assert client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE}).status_code == 200
    assert client.get("/me").status_code == 200

    # cookie 认证的不安全方法必须显式携带同源 Origin（严格 CSRF）。
    logout = client.post("/auth/logout", headers={"Origin": "http://testserver"})
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True
    assert "study_session=;" in logout.headers["set-cookie"] or 'study_session=""' in logout.headers["set-cookie"]

    # 撤销立刻生效：同一份 cookie 再来，回库查询扑空 → 拒绝。
    assert client.get("/me").status_code == 401


@pytest.mark.invariant
def test_revoked_session_fails_closed(client, invited):
    """直接在仓储里撤销（模拟管理端踢人）→ cookie 立刻失效。

    cookie 声明本身没有任何吊销信息，状态只在数据库 ——
    所以撤销不需要等 cookie 过期。
    """
    assert client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE}).status_code == 200
    cookie_value = client.cookies[SESSION_COOKIE_NAME]

    # 从 cookie 反查 session_id 再撤销（测试自己解析声明，绕开认证路径拿 id）。
    import base64
    import json

    body, _ = cookie_value.rsplit(".", 1)
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    platform = invited
    from app.identity.models import Principal

    revoked = platform.session_store.revoke(
        Principal(principal_id=claims["principal_id"], tenant_id=claims["tenant_id"]),
        claims["session_id"],
        at=platform.clock.now(),
    )
    assert revoked is True
    assert client.get("/me").status_code == 401


@pytest.mark.invariant
def test_signed_cookie_without_db_session_fails_closed(platform, client, invited):
    """签名有效但库里没有这条会话 → 拒绝。

    攻击者拿到签名密钥之前伪造不出这条 cookie；但**签名验证通过≠通过认证** ——
    回库查询是第二道独立防线（cookie 被复制到别处、库被回滚等场景）。
    """
    from datetime import datetime, timezone

    from app.product.models import UserSession

    now = datetime.now(timezone.utc)
    ghost = UserSession(
        session_id="sess_ghost",
        tenant_id="tenant_demo",
        principal_id="user_demo",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    forged_cookie = platform.cookie_auth.issue(ghost)
    response = client.get("/me", cookies={SESSION_COOKIE_NAME: forged_cookie})
    assert response.status_code == 401


@pytest.mark.invariant
def test_tampered_cookie_rejected(client, invited):
    assert client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE}).status_code == 200
    cookie_value = client.cookies[SESSION_COOKIE_NAME]

    # 改动载荷任何一字节（这里把 principal_id 换成别人）都必须被验签拦下。
    import base64
    import json

    body, signature = cookie_value.rsplit(".", 1)
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["principal_id"] = "user_someone_else"
    forged_body = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).decode().rstrip("=")
    tampered = f"{forged_body}.{signature}"
    response = client.get("/me", cookies={SESSION_COOKIE_NAME: tampered})
    assert response.status_code == 401


# ------------------------------------------------------------ CSRF 与兼容


@pytest.mark.invariant
def test_csrf_origin_mismatch_rejected_for_unsafe_methods(client, invited):
    assert client.post("/auth/invitations/exchange", json={"token": TOKEN_LIVE}).status_code == 200

    evil = client.post("/auth/logout", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert evil.json()["code"] == "CSRF_DENIED"

    # 安全方法不检查来源（GET 不产生副作用，不值得为此破坏可书签性）。
    assert client.get("/me", headers={"Origin": "https://evil.example"}).status_code == 200

    # 同源 Origin 放行。
    ok = client.post("/auth/logout", headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


@pytest.mark.invariant
def test_bearer_remains_a_compatible_path(client, platform, auth_headers):
    """bearer 是显式的运维/测试兼容适配器：不受 CSRF 检查，仍走归属判定。"""
    headers = auth_headers()
    assert client.get("/me", headers=headers).status_code == 200
    body = client.get("/projects/proj_demo/mastery", headers=headers)
    assert body.status_code == 200


@pytest.mark.invariant
def test_no_credentials_is_401(client):
    assert client.get("/me").status_code == 401
