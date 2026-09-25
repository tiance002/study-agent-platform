"""项目 CRUD 的 API 级测试（任务 4）。

全部走 cookie 认证路径（生产主路径）：**真实注册** → 种 cookie → 带 cookie 调用。
涉及不安全方法的用例带同源 `Origin` —— CSRF 严格模式下的必要姿态。
"""

from __future__ import annotations

import uuid

import pytest
from app.identity.cookie_auth import SESSION_COOKIE_NAME

ORIGIN = {"Origin": "http://testserver"}


def _keyed() -> dict:
    """写端点必须携带 Idempotency-Key（审查修复）；每次调用唯一。"""
    return {**ORIGIN, "Idempotency-Key": "proj-" + uuid.uuid4().hex}


@pytest.fixture
def cookie_user(client, register_user):
    """真实注册一个用户并把其会话 cookie 放进客户端，返回 client。"""
    _, account = register_user()
    client.cookies.set(SESSION_COOKIE_NAME, account.cookie)
    return client


# ------------------------------------------------------------ 创建与列表


@pytest.mark.invariant
def test_create_project_grants_creator_immediately(cookie_user):
    created = cookie_user.post("/projects", json={"name": "审计入门", "goal": "学会看日志"}, headers=_keyed())
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
    assert cookie_user.post("/projects", json={"name": ""}, headers=_keyed()).status_code == 422
    response = cookie_user.post(
        "/projects",
        json={"name": "x", "tenant_id": "tenant_evil"},
        headers=_keyed(),
    )
    assert response.status_code == 422, "归属字段出现在请求体必须当场被拒"


# ------------------------------------------------------------ 乐观锁


@pytest.mark.invariant
def test_patch_optimistic_lock_flow(cookie_user):
    project_id = cookie_user.post(
        "/projects", json={"name": "初版"}, headers=_keyed()
    ).json()["project_id"]

    renamed = cookie_user.patch(
        f"/projects/{project_id}",
        json={"name": "第二版", "expected_version": 1},
        headers=_keyed(),
    )
    assert renamed.status_code == 200
    assert renamed.json()["version"] == 2
    assert renamed.json()["name"] == "第二版"

    stale = cookie_user.patch(
        f"/projects/{project_id}",
        json={"name": "迟到的编辑", "expected_version": 1},
        headers=_keyed(),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"

    current = cookie_user.get(f"/projects/{project_id}").json()
    assert current["name"] == "第二版", "冲突不产生部分改动"


@pytest.mark.invariant
def test_patch_requires_at_least_one_field(cookie_user):
    project_id = cookie_user.post(
        "/projects", json={"name": "x"}, headers=_keyed()
    ).json()["project_id"]
    response = cookie_user.patch(
        f"/projects/{project_id}", json={"expected_version": 1}, headers=_keyed()
    )
    assert response.status_code == 422 or response.json()["code"] == "PARAMS_INVALID"


# ------------------------------------------------------------ 隔离


@pytest.mark.invariant
def test_projects_are_invisible_to_other_users(register_user, cookie_user):
    """另一个用户看不到、改不了别人的项目 —— 统一 404。"""
    project_id = cookie_user.post(
        "/projects", json={"name": "我的项目"}, headers=_keyed()
    ).json()["project_id"]

    other, _ = register_user()
    other_projects = {p["project_id"] for p in other.get("/projects").json()["projects"]}
    assert project_id not in other_projects, "未授予者看不到别人的项目"
    assert other.get(f"/projects/{project_id}").status_code == 404
    patched = other.patch(
        f"/projects/{project_id}",
        json={"name": "抢改", "expected_version": 1},
        headers=_keyed(),
    )
    assert patched.status_code == 404


@pytest.mark.invariant
def test_endpoints_require_authentication(client):
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={"name": "x"}).status_code == 401
