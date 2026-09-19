"""Round 2 PostgreSQL exit gate: complete cookie-user product flow survives restart."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import timedelta

import psycopg
import pytest
from app.deployment import DeploymentSettings
from app.identity.ports import SystemContext
from app.main import build_platform, create_app
from fastapi.testclient import TestClient

MIGRATION_DSN = os.environ.get(
    "STUDY_PLATFORM_MIGRATION_DSN",
    "postgresql://postgres@127.0.0.1:5432/study_platform",
)
ORIGIN = "http://testserver"


def _reachable() -> bool:
    try:
        with psycopg.connect(MIGRATION_DSN, connect_timeout=2):
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
    tenant_id = "t_r2_" + suffix
    principal_id = "u_r2_" + suffix
    token = "invite-r2-" + suffix

    with psycopg.connect(MIGRATION_DSN) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                (tenant_id, "Round 2 exit gate"),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
                (principal_id, tenant_id),
            )

    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_PERSISTENCE": "postgres",
            "STUDY_PLATFORM_EXCHANGE_LIMIT": "100000",
        }
    )
    platform_a = build_platform(var_dir=tmp_path / "a", settings=settings)
    now = platform_a.clock.now()
    platform_a.invitations.issue(
        SystemContext(tenant_id, "Round 2 exit gate"),
        invitation_id="inv_" + suffix,
        token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        issued_by=principal_id,
        invitee_principal_id=principal_id,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )

    client_a = TestClient(create_app(platform=platform_a))
    exchanged = client_a.post("/auth/invitations/exchange", json={"token": token})
    assert exchanged.status_code == 200, exchanged.text
    cookie = exchanged.cookies["study_session"]

    project_key = "r2-project-" + suffix
    project_body = {"name": "Agent 工程首版", "goal": "完成可用学习闭环"}
    project_response = client_a.post(
        "/projects", json=project_body, headers=_key(project_key)
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project_id"]

    conversation = client_a.post(
        f"/projects/{project_id}/conversations",
        json={"title": "第一周"},
        headers=_key("r2-conversation-" + suffix),
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
    assert client_b.get(f"/projects/{project_id}/plan").json()["plan"]["goal"] == project_body[
        "goal"
    ]
    assert client_b.get(f"/projects/{project_id}/sources").json()["sources"][0][
        "display_name"
    ] == "FastAPI 文档"

    replay = client_b.post("/projects", json=project_body, headers=_key(project_key))
    assert replay.status_code == 201
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == project_response.json()
    assert len(client_b.get("/projects").json()["projects"]) == 1
