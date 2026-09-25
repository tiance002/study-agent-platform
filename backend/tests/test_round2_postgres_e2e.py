"""Round 2 PostgreSQL exit gate: complete cookie-user product flow survives restart."""

from __future__ import annotations

import uuid

import pg_support
import psycopg
import pytest
from app.deployment import DeploymentSettings
from app.main import build_platform, create_app
from fastapi.testclient import TestClient

ORIGIN = "http://testserver"


def _reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _reachable(), reason="本地 PostgreSQL 未运行"),
]


def _key(value: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Idempotency-Key": value}


@pytest.mark.invariant
def test_cookie_product_flow_and_idempotency_survive_restart(tmp_path):
    suffix = uuid.uuid4().hex

    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_PERSISTENCE": "postgres",
            "STUDY_PLATFORM_AUTH_ATTEMPT_LIMIT": "100000",
        }
    )
    platform_a = build_platform(var_dir=tmp_path / "a", settings=settings)

    # 身份一律通过真实 HTTP 注册取得：注册原子地建租户/主体/凭据/会话/默认项目。
    client_a = TestClient(create_app(platform=platform_a))
    registered = client_a.post(
        "/auth/register",
        json={"username": "r2-" + suffix[:10], "password": "r2-pass-1234"},
        headers={"Origin": ORIGIN},
    )
    assert registered.status_code == 201, registered.text
    cookie = client_a.cookies["study_session"]
    project_id = registered.json()["default_project_id"]

    conversation_key = "r2-conversation-" + suffix
    conversation = client_a.post(
        f"/projects/{project_id}/conversations",
        json={"title": "第一周"},
        headers=_key(conversation_key),
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]

    message = client_a.post(
        f"/projects/{project_id}/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "从认证和幂等开始"},
        headers=_key("r2-message-" + suffix),
    )
    assert message.status_code == 201, message.text

    plan = client_a.put(
        f"/projects/{project_id}/plan",
        json={
            "goal": "完成可用学习闭环",
            "milestones": [
                {
                    "title": "可靠 API",
                    "description": "先闭合持久化边界",
                    "tasks": [{"title": "完成 PostgreSQL 闭环"}],
                }
            ],
        },
        headers=_key("r2-plan-" + suffix),
    )
    assert plan.status_code == 200, plan.text

    source = client_a.post(
        f"/projects/{project_id}/sources",
        json={
            "display_name": "FastAPI 文档",
            "media_type": "text/html",
            "acquisition": {"url": "https://fastapi.tiangolo.com/"},
        },
        headers=_key("r2-source-" + suffix),
    )
    assert source.status_code == 201, source.text

    # Reconstruct every adapter. Only PostgreSQL and the signed cookie carry state.
    platform_b = build_platform(var_dir=tmp_path / "b", settings=settings)
    client_b = TestClient(create_app(platform=platform_b))
    client_b.cookies.set("study_session", cookie)

    projects = client_b.get("/projects")
    assert projects.status_code == 200
    assert [item["project_id"] for item in projects.json()["projects"]] == [project_id]

    messages = client_b.get(
        f"/projects/{project_id}/conversations/{conversation_id}/messages"
    )
    assert [item["content"] for item in messages.json()["messages"]] == [
        "从认证和幂等开始"
    ]
    assert client_b.get(f"/projects/{project_id}/plan").json()["plan"]["goal"] == "完成可用学习闭环"
    assert client_b.get(f"/projects/{project_id}/sources").json()["sources"][0][
        "display_name"
    ] == "FastAPI 文档"

    replay = client_b.post(
        f"/projects/{project_id}/conversations",
        json={"title": "第一周"},
        headers=_key(conversation_key),
    )
    assert replay.status_code == 201
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == conversation.json()
    assert len(client_b.get("/projects").json()["projects"]) == 1
