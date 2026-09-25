"""Round 8: the HTTP teaching workflow survives reconstructed web/worker state."""

from __future__ import annotations

import json
import uuid

import pg_support
import psycopg
import pytest
from app.budget.platform import reservation_id_for_run
from app.deployment import DeploymentSettings
from app.main import build_platform, create_app
from app.teaching.provider import ScriptedProvider
from app.workers.ingestion import run_once as run_ingestion_once
from app.workers.teaching import run_once as run_teaching_once
from fastapi.testclient import TestClient


def _reachable() -> bool:
    return pg_support.reachable()


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _reachable(), reason="本地 PostgreSQL 未运行"),
]


@pytest.fixture
def enabled_platform_budget(pg_database):
    dsn = pg_support.migration_dsn()
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT monthly_cap_micro, paid_dispatch_enabled"
            " FROM platform_budget_config WHERE config_id = true"
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE platform_budget_config"
            " SET monthly_cap_micro = NULL, paid_dispatch_enabled = true"
            " WHERE config_id = true"
        )
        conn.commit()
    try:
        yield
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE platform_budget_config"
                " SET monthly_cap_micro = %s, paid_dispatch_enabled = %s"
                " WHERE config_id = true",
                row,
            )
            conn.commit()


def test_http_teaching_and_ingestion_survive_reconstructed_platform(
    tmp_path, enabled_platform_budget
):
    suffix = uuid.uuid4().hex[:10]
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_PERSISTENCE": "postgres",
            "STUDY_PLATFORM_REGISTRATION_ENABLED": "true",
            "STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED": "true",
            "STUDY_PLATFORM_AUTH_ATTEMPT_LIMIT": "100000",
            "STUDY_PLATFORM_TEACHING_PROVIDER": "openai",
            "STUDY_PLATFORM_TEACHING_MODEL": "round8-test-model",
            "STUDY_PLATFORM_TEACHING_API_KEY": "round8-test-key",
        }
    )
    platform_a = build_platform(var_dir=tmp_path / "web-a", settings=settings)
    client_a = TestClient(create_app(platform=platform_a))
    headers = {"Origin": "http://testserver"}

    registered = client_a.post(
        "/auth/register",
        json={"username": "r8" + suffix, "password": "Round8Pass12"},
        headers=headers,
    )
    assert registered.status_code == 201, registered.text
    project_id = registered.json()["default_project_id"]

    conversation = client_a.post(
        f"/projects/{project_id}/conversations",
        json={"title": "重启恢复会话"},
        headers={**headers, "Idempotency-Key": "r8-conv-" + suffix},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]

    plan = client_a.put(
        f"/projects/{project_id}/plan",
        json={
            "goal": "验证重启恢复",
            "milestones": [{"title": "恢复教学运行", "description": "", "tasks": []}],
        },
        headers={**headers, "Idempotency-Key": "r8-plan-" + suffix},
    )
    assert plan.status_code == 200, plan.text

    source = client_a.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "恢复资料", "acquisition": {"kind": "r8"}},
        headers={**headers, "Idempotency-Key": "r8-source-" + suffix},
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["source_id"]
    uploaded = client_a.post(
        f"/projects/{project_id}/sources/{source_id}/content",
        json={
            "title": "恢复资料",
            "content": "重启后仍然可以读取这份资料。",
            "media_type": "text/plain",
            "language": "zh",
        },
        headers={**headers, "Idempotency-Key": "r8-content-" + suffix},
    )
    assert uploaded.status_code == 202, uploaded.text
    job_id = uploaded.json()["job"]["job_id"]
    assert run_ingestion_once(platform_a, worker_id="r8-ingestion").kind == "succeeded"

    platform_a.teaching_provider = ScriptedProvider([])
    created = client_a.post(
        f"/projects/{project_id}/conversations/{conversation_id}/teaching-runs",
        json={"question": "重启后还能继续回答吗？"},
        headers={**headers, "Idempotency-Key": "r8-run-" + suffix},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    cookie = client_a.cookies["study_session"]

    # Reconstruct both adapters and the HTTP application. Only PostgreSQL and
    # the signed cookie carry the queued run, source, plan, and conversation.
    platform_b = build_platform(var_dir=tmp_path / "web-b", settings=settings)
    platform_b.teaching_provider = ScriptedProvider(
        [
            json.dumps(
                {"answer_markdown": "重启后仍然可以继续回答。", "citations": []},
                ensure_ascii=False,
            )
        ]
    )
    client_b = TestClient(create_app(platform=platform_b))
    client_b.cookies.set("study_session", cookie)

    assert client_b.get(f"/projects/{project_id}/plan").status_code == 200
    job = client_b.get(f"/projects/{project_id}/ingestion-jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    assert client_b.get(f"/projects/{project_id}/conversations/{conversation_id}/messages").json()[
        "messages"
    ][0]["role"] == "user"

    assert run_teaching_once(platform_b, worker_id="r8-teaching") == "succeeded"
    status = client_b.get(f"/projects/{project_id}/teaching-runs/{run_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    messages = client_b.get(
        f"/projects/{project_id}/conversations/{conversation_id}/messages"
    ).json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert run_teaching_once(platform_b, worker_id="r8-teaching-again") == "idle"

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM teaching_runs WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 1
        teaching_reservation = conn.execute(
            "SELECT state, actual_micro FROM teaching_reservations WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert teaching_reservation is not None
        assert teaching_reservation[0] == "settled"
        assert teaching_reservation[1] > 0
        platform_reservation = conn.execute(
            "SELECT state, actual_spend_micro FROM platform_paid_reservations"
            " WHERE reservation_id = %s",
            (reservation_id_for_run(run_id),),
        ).fetchone()
        assert platform_reservation is not None
        assert platform_reservation[0] == "settled"
        assert platform_reservation[1] > 0
