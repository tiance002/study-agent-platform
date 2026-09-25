"""产品 API 的端到端测试（任务 5）：会话 → 消息 → 计划 → 资料。

全部走 cookie 认证主路径。退出标准的核心链路在这里：
「建项目 → 开会话 → 发消息 → 保存计划 → 登记资料」全程无终端、无令牌。
"""

from __future__ import annotations

import uuid

import pytest

ORIGIN = {"Origin": "http://testserver"}


def _keyed() -> dict:
    """写端点必须携带 Idempotency-Key（审查修复）；每次调用唯一，
    否则同键同内容会命中重放缓存，测不到真实执行。"""
    return {**ORIGIN, "Idempotency-Key": "prod-" + uuid.uuid4().hex}


@pytest.fixture
def user_with_project(client, register_user):
    """真实注册的登录用户 + 一个已创建的项目，返回 (client, project_id)。"""
    _, account = register_user()
    client.cookies.set("study_session", account.cookie)
    created = client.post("/projects", json={"name": "产品 API 项目"}, headers=_keyed())
    assert created.status_code == 201
    return client, created.json()["project_id"]


PLAN_BODY = {
    "goal": "三周学会给 AI 写评审",
    "milestones": [
        {
            "title": "第一周：读规格",
            "tasks": [{"title": "读 01 号规格"}, {"title": "写摘要"}],
        },
        {"title": "第二周：动手", "tasks": [{"title": "提交一个补丁"}]},
    ],
}


# ------------------------------------------------------------ 会话与消息


@pytest.mark.invariant
def test_conversation_message_flow(user_with_project):
    client, project_id = user_with_project

    conversation = client.post(
        f"/projects/{project_id}/conversations", json={"title": "问答"}, headers=_keyed()
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["conversation_id"]

    first = client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "第一问"},
        headers=_keyed(),
    )
    assert first.status_code == 201
    assert first.json()["seq"] == 1

    reply = client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/messages",
        json={"role": "assistant", "content": "第一答"},
        headers=_keyed(),
    )
    assert reply.json()["seq"] == 2, "seq 由服务端分配，请求体没有这个字段可传"

    listed = client.get(
        f"/projects/{project_id}/conversations/{conversation_id}/messages"
    )
    assert [m["seq"] for m in listed.json()["messages"]] == [1, 2]


@pytest.mark.invariant
def test_message_role_is_closed_set(user_with_project):
    client, project_id = user_with_project
    conversation_id = client.post(
        f"/projects/{project_id}/conversations", json={}, headers=_keyed()
    ).json()["conversation_id"]
    response = client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/messages",
        json={"role": "admin", "content": "非法角色"},
        headers=_keyed(),
    )
    assert response.status_code == 422, "角色是闭集：未知角色不能被当成 user 放过去"


# ------------------------------------------------------------ 计划


@pytest.mark.invariant
def test_plan_replace_and_current(user_with_project):
    client, project_id = user_with_project

    empty = client.get(f"/projects/{project_id}/plan")
    assert empty.status_code == 204, "没有计划是 204，不是 404 也不是空对象"

    first = client.put(f"/projects/{project_id}/plan", json=PLAN_BODY, headers=_keyed())
    assert first.status_code == 200
    body = first.json()
    assert body["plan"]["version"] == 1
    assert len(body["milestones"]) == 2
    assert len(body["tasks"]) == 3
    # 服务端生成的 id 一应俱全。
    assert all(m["milestone_id"] for m in body["milestones"])

    second = client.put(
        f"/projects/{project_id}/plan",
        json={**PLAN_BODY, "goal": "第四周收尾", "milestones": [
            {"title": "冲刺", "tasks": [{"title": "总复习"}]},
        ]},
        headers=_keyed(),
    )
    assert second.json()["plan"]["version"] == 2
    assert len(second.json()["milestones"]) == 1, "整版替换：旧里程碑不残留"

    current = client.get(f"/projects/{project_id}/plan")
    assert current.json()["plan"]["version"] == 2
    assert current.json()["plan"]["goal"] == "第四周收尾"

    history = client.get(f"/projects/{project_id}/plan/history")
    assert [p["version"] for p in history.json()["plans"]] == [1, 2]


# ------------------------------------------------------------ 资料


@pytest.mark.invariant
def test_source_registration_is_idempotent_on_same_acquisition(user_with_project):
    client, project_id = user_with_project
    acquisition = {"kind": "upload", "file_name": "讲义.pdf"}

    first = client.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "讲义.pdf", "media_type": "application/pdf", "acquisition": acquisition},
        headers=_keyed(),
    )
    assert first.status_code == 201
    second = client.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "讲义(重传).pdf", "acquisition": acquisition},
        headers=_keyed(),
    )
    assert second.status_code == 201
    assert second.json()["source_id"] == first.json()["source_id"], (
        "同一 acquisition 视为同一资料：幂等成功而非报错"
    )
    assert len(client.get(f"/projects/{project_id}/sources").json()["sources"]) == 1


# ------------------------------------------------------------ 隔离


@pytest.mark.invariant
def test_products_are_invisible_to_other_users(register_user, user_with_project):
    client_a, project_id = user_with_project

    conversation_id = client_a.post(
        f"/projects/{project_id}/conversations", json={}, headers=_keyed()
    ).json()["conversation_id"]

    other, _ = register_user()
    assert other.get(f"/projects/{project_id}/conversations").status_code == 404
    assert (
        other.post(
            f"/projects/{project_id}/conversations/{conversation_id}/messages",
            json={"role": "user", "content": "越权"},
            headers=_keyed(),
        ).status_code
        == 404
    )
    assert other.put(f"/projects/{project_id}/plan", json=PLAN_BODY, headers=_keyed()).status_code == 404
    assert other.post(
        f"/projects/{project_id}/sources", json={"display_name": "x"}, headers=_keyed()
    ).status_code == 404
