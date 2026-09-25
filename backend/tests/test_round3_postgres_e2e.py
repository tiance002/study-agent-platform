"""Round 3 PostgreSQL exit gate: cookie user reaches verified mastery after restart."""

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
    pytest.mark.invariant,
    pytest.mark.skipif(not _reachable(), reason="本地 PostgreSQL 未运行"),
]


def _headers(key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Idempotency-Key": key}


def test_learning_loop_survives_restart_and_state_does_not_forge_mastery(tmp_path):
    suffix = uuid.uuid4().hex
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_PERSISTENCE": "postgres", "STUDY_PLATFORM_AUTH_ATTEMPT_LIMIT": "100000"}
    )
    platform_a = build_platform(var_dir=tmp_path / "a", settings=settings)

    # 身份一律通过真实 HTTP 注册取得。
    client_a = TestClient(create_app(platform=platform_a))
    registered = client_a.post(
        "/auth/register",
        json={"username": "r3-" + suffix[:10], "password": "r3-pass-1234"},
        headers={"Origin": ORIGIN},
    )
    assert registered.status_code == 201, registered.text
    cookie = client_a.cookies["study_session"]

    project = client_a.post(
        "/projects",
        headers=_headers("r3-project-" + suffix),
        json={"name": "首个闭环", "goal": "理解数据库事务"},
    )
    project_id = project.json()["project_id"]
    diagnosis = client_a.post(
        f"/projects/{project_id}/diagnosis",
        headers=_headers("r3-diagnosis-" + suffix),
        json={
            "experience_level": "beginner",
            "weekly_hours": 5,
            "preferred_style": "mixed",
        },
    )
    assert diagnosis.status_code == 201, diagnosis.text
    generated = client_a.post(
        f"/projects/{project_id}/plan/generate",
        headers=_headers("r3-generate-" + suffix),
        json={},
    )
    assert generated.status_code == 201, generated.text
    task_id = generated.json()["tasks"][0]["task_id"]
    assert client_a.post(
        f"/projects/{project_id}/tasks/{task_id}/transition",
        headers=_headers("r3-start-" + suffix),
        json={"expected_status": "pending", "next_status": "in_progress"},
    ).status_code == 200
    assert client_a.post(
        f"/projects/{project_id}/tasks/{task_id}/submissions",
        headers=_headers("r3-submit-" + suffix),
        json={"mode": "self_report", "content": "我能解释提交和回滚。"},
    ).status_code == 201

    platform_b = build_platform(var_dir=tmp_path / "b", settings=settings)
    client_b = TestClient(create_app(platform=platform_b))
    client_b.cookies.set("study_session", cookie)
    mastery_before = client_b.get(f"/projects/{project_id}/mastery").json()
    concept = next(row for row in mastery_before["components"] if row["component_id"] == "concept")
    # 自报是中性证据：被记录但不抬升独立水平（self-report != verified）。
    assert (concept["independence_level"], concept["confidence"]) == ("unknown", "low")
    detail = client_b.get(f"/projects/{project_id}/tasks/{task_id}").json()
    assert detail["verified"] is False
    assert detail["self_reported"] is True
    assert client_b.get(f"/projects/{project_id}/diagnosis").json() == diagnosis.json()

    done = client_b.post(
        f"/projects/{project_id}/tasks/{task_id}/transition",
        headers=_headers("r3-done-" + suffix),
        json={"expected_status": "in_progress", "next_status": "done"},
    )
    assert done.status_code == 200
    assert client_b.get(f"/projects/{project_id}/mastery").json() == mastery_before
