"""cookie 会话的 HTTP 级语义（登录、Cookie 标志、撤销、篡改、CSRF）。

这些用例原在 `test_invitation_auth.py` 中，邀请码移除后改指**密码注册/登录**
流程：语义（撤销立刻生效、签名有效≠通过认证、统一拒绝）本身与邀请无关，
是 cookie 会话主路径的共享证明，因此单独成文。

全部走完整 HTTP 路径（TestClient），身份一律通过真实注册/登录取得。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

import pytest
from app.identity.cookie_auth import SESSION_COOKIE_NAME
from app.identity.models import Principal
from app.product.models import UserSession
from fastapi.testclient import TestClient

SAME_ORIGIN = "http://testserver"


def _claims_from_cookie(cookie_value: str) -> dict:
    body, _ = cookie_value.rsplit(".", 1)
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


# ------------------------------------------------------------ 注册与登录


@pytest.mark.invariant
def test_register_sets_cookie_and_me_works(client):
    username = "u" + uuid.uuid4().hex[:10]
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "test-pass-1"},
        headers={"Origin": SAME_ORIGIN},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["principal_id"]
    assert body["default_project_id"], "注册自动创建默认项目"
    # 口令与哈希**不出现在任何响应里**。
    assert "test-pass-1" not in response.text

    # cookie 标志：HttpOnly / SameSite。这些不是锦上添花，是威胁模型的落点。
    set_cookie = response.headers["set-cookie"]
    assert "httponly" in set_cookie.lower(), "HttpOnly 缺失 = XSS 能偷走会话"
    assert "samesite=lax" in set_cookie.lower()

    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["principal_id"] == body["principal_id"]


@pytest.mark.invariant
def test_login_after_logout_returns_a_new_session(client):
    username = "u" + uuid.uuid4().hex[:10]
    assert client.post(
        "/auth/register",
        json={"username": username, "password": "test-pass-1"},
        headers={"Origin": SAME_ORIGIN},
    ).status_code == 201
    assert client.post("/auth/logout", headers={"Origin": SAME_ORIGIN}).status_code == 200
    assert client.get("/me").status_code == 401

    logged_in = client.post(
        "/auth/login",
        json={"username": username, "password": "test-pass-1"},
        headers={"Origin": SAME_ORIGIN},
    )
    assert logged_in.status_code == 200
    assert client.get("/me").status_code == 200


# ------------------------------------------------------------ 会话撤销


@pytest.mark.invariant
def test_logout_revokes_session_and_clears_cookie(client):
    username = "u" + uuid.uuid4().hex[:10]
    client.post(
        "/auth/register",
        json={"username": username, "password": "test-pass-1"},
        headers={"Origin": SAME_ORIGIN},
    )
    assert client.get("/me").status_code == 200

    # cookie 认证的不安全方法必须显式携带同源 Origin（严格 CSRF）。
    logout = client.post("/auth/logout", headers={"Origin": SAME_ORIGIN})
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True
    assert "study_session=;" in logout.headers["set-cookie"] or 'study_session=""' in logout.headers["set-cookie"]

    # 撤销立刻生效：同一份 cookie 再来，回库查询扑空 → 拒绝。
    assert client.get("/me").status_code == 401


@pytest.mark.invariant
def test_revoked_session_fails_closed(client, platform, register_user):
    """直接在仓储里撤销（模拟管理端踢人）→ cookie 立刻失效。

    cookie 声明本身没有任何吊销信息，状态只在数据库 ——
    所以撤销不需要等 cookie 过期。
    """
    _, account = register_user()
    claims = _claims_from_cookie(account.cookie)
    revoked = platform.session_store.revoke(
        Principal(principal_id=claims["principal_id"], tenant_id=claims["tenant_id"]),
        claims["session_id"],
        at=platform.clock.now(),
    )
    assert revoked is True
    assert TestClient(client.app).get(
        "/me", cookies={SESSION_COOKIE_NAME: account.cookie}
    ).status_code == 401


@pytest.mark.invariant
def test_signed_cookie_without_db_session_fails_closed(platform, client):
    """签名有效但库里没有这条会话 → 拒绝。

    攻击者拿到签名密钥之前伪造不出这条 cookie；但**签名验证通过≠通过认证** ——
    回库查询是第二道独立防线（cookie 被复制到别处、库被回滚等场景）。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ghost = UserSession(
        session_id="sess_ghost",
        tenant_id="tenant_demo",
        principal_id="user_demo",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        credential_id="cred_ghost",
        security_generation=1,
    )
    forged_cookie = platform.cookie_auth.issue(ghost)
    response = client.get("/me", cookies={SESSION_COOKIE_NAME: forged_cookie})
    assert response.status_code == 401


@pytest.mark.invariant
def test_tampered_cookie_rejected(client, register_user):
    _, account = register_user()
    claims = _claims_from_cookie(account.cookie)

    # 改动载荷任何一字节（这里把 principal_id 换成别人）都必须被验签拦下。
    body, signature = account.cookie.rsplit(".", 1)
    claims["principal_id"] = "user_someone_else"
    forged_body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tampered = f"{forged_body}.{signature}"
    response = TestClient(client.app).get("/me", cookies={SESSION_COOKIE_NAME: tampered})
    assert response.status_code == 401


# ------------------------------------------------------------ CSRF 与兼容


@pytest.mark.invariant
def test_csrf_origin_mismatch_rejected_for_unsafe_methods(client, register_user):
    _, account = register_user()
    client.cookies.set(SESSION_COOKIE_NAME, account.cookie)

    evil = client.post("/auth/logout", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert evil.json()["code"] == "CSRF_DENIED"

    # 安全方法不检查来源（GET 不产生副作用，不值得为此破坏可书签性）。
    assert client.get("/me", headers={"Origin": "https://evil.example"}).status_code == 200

    # 同源 Origin 放行。
    ok = client.post("/auth/logout", headers={"Origin": SAME_ORIGIN})
    assert ok.status_code == 200


@pytest.mark.invariant
def test_bearer_remains_a_compatible_path(client, platform):
    """bearer 是显式的运维/测试兼容适配器：不受 CSRF 检查，仍走归属判定。"""
    token = platform.sessions.issue(
        principal_id="user_demo",
        tenant_id="tenant_demo",
        issued_at=platform.clock.now(),
    )
    headers = {"Authorization": f"Bearer {platform.sessions.serialize(token)}"}
    assert client.get("/me", headers=headers).status_code == 200


@pytest.mark.invariant
def test_no_credentials_is_401(client):
    assert client.get("/me").status_code == 401
