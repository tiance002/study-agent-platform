"""项目 CRUD 的 API 级测试（任务 4）。

全部走 cookie 认证路径（生产主路径）：邀请兑换 → 种 cookie → 带 cookie 调用。
涉及不安全方法的用例带同源 `Origin` —— CSRF 严格模式下的必要姿态。
"""

from __future__ import annotations

import uuid

import pytest
from app.identity.ports import SystemContext
from app.main import DEMO_PRINCIPAL, DEMO_TENANT

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def cookie_user(client, platform):
    """邀请兑换出一个已登录用户（演示租户成员），返回 client（cookie 已种）。"""
    token = "proj-api-" + uuid.uuid4().hex
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "项目 API 测试"),
        invitation_id="inv_" + uuid.uuid4().hex[:8],
        token_hash="sha256:" + __import__("hashlib").sha256(token.encode()).hexdigest(),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + __import__("datetime").timedelta(days=1),
    )
    response = client.post("/auth/invitations/exchange", json={"token": token})
    assert response.status_code == 200
    return client


# ------------------------------------------------------------ 创建与列表


@pytest.mark.invariant
def test_create_project_grants_creator_immediately(cookie_user):
    created = cookie_user.post("/projects", json={"name": "审计入门", "goal": "学会看日志"}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "审计入门"
    assert body["version"] == 1

    listed = cookie_user.get("/projects")
    ids = {p["project_id"] for p in listed.json()["projects"]}
    assert body["project_id"] in ids, "创建即授予：列表里必须立刻可见"

    detail = cookie_user.get(f"/projects/{body['project_id']}")
    assert detail.status_code == 200
    assert detail.json()["goal"] == "学会看日志"


@pytest.mark.invariant
def test_create_rejects_blank_name_and_extra_fields(cookie_user):
    assert cookie_user.post("/projects", json={"name": ""}, headers=ORIGIN).status_code == 422
    response = cookie_user.post(
        "/projects",
        json={"name": "x", "tenant_id": "tenant_evil"},
        headers=ORIGIN,
    )
    assert response.status_code == 422, "归属字段出现在请求体必须当场被拒"


# ------------------------------------------------------------ 乐观锁


@pytest.mark.invariant
def test_patch_optimistic_lock_flow(cookie_user):
    project_id = cookie_user.post(
        "/projects", json={"name": "初版"}, headers=ORIGIN
    ).json()["project_id"]

    renamed = cookie_user.patch(
        f"/projects/{project_id}",
        json={"name": "第二版", "expected_version": 1},
        headers=ORIGIN,
    )
    assert renamed.status_code == 200
    assert renamed.json()["version"] == 2
    assert renamed.json()["name"] == "第二版"

    stale = cookie_user.patch(
        f"/projects/{project_id}",
        json={"name": "迟到的编辑", "expected_version": 1},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"

    current = cookie_user.get(f"/projects/{project_id}").json()
    assert current["name"] == "第二版", "冲突不产生部分改动"


@pytest.mark.invariant
def test_patch_requires_at_least_one_field(cookie_user):
    project_id = cookie_user.post(
        "/projects", json={"name": "x"}, headers=ORIGIN
    ).json()["project_id"]
    response = cookie_user.patch(
        f"/projects/{project_id}", json={"expected_version": 1}, headers=ORIGIN
    )
    assert response.status_code == 422 or response.json()["code"] == "PARAMS_INVALID"


# ------------------------------------------------------------ 隔离


@pytest.mark.invariant
def test_projects_are_invisible_to_other_users(platform, client, cookie_user):
    """同租户的另一个用户看不到、改不了别人的项目 —— 统一 404。"""
    project_id = cookie_user.post(
        "/projects", json={"name": "我的项目"}, headers=ORIGIN
    ).json()["project_id"]

    other_token = "other-user-" + uuid.uuid4().hex
    import hashlib
    from datetime import timedelta

    from app.main import DEMO_TENANT

    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "第二用户"),
        invitation_id="inv_" + uuid.uuid4().hex[:8],
        token_hash="sha256:" + hashlib.sha256(other_token.encode()).hexdigest(),
        issued_by="seed",
        invitee_principal_id="user_other",
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    other = _login_second_user(client, other_token)
    assert other.get("/projects").json()["projects"] == [], "未授予者列表为空"
    assert other.get(f"/projects/{project_id}").status_code == 404
    patched = other.patch(
        f"/projects/{project_id}",
        json={"name": "抢改", "expected_version": 1},
        headers=ORIGIN,
    )
    assert patched.status_code == 404


def _login_second_user(existing_client, token: str):
    """第二个用户的客户端：独立 cookie jar，复用同一个 app 实例。"""
    from fastapi.testclient import TestClient

    new_client = TestClient(existing_client.app)
    response = new_client.post("/auth/invitations/exchange", json={"token": token})
    assert response.status_code == 200
    return new_client


@pytest.mark.invariant
def test_endpoints_require_authentication(client):
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={"name": "x"}).status_code == 401
