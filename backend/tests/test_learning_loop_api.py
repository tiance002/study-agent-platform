# -*- coding: utf-8 -*-
"""第 3 轮用户路径：诊断 -> 生成计划 -> 提交 -> 掌握投影。"""

from __future__ import annotations


def _headers(auth_headers, key: str) -> dict[str, str]:
    return {**auth_headers(), "Idempotency-Key": key}


def test_learning_loop_reaches_first_verified_task(
    client, auth_headers, demo
):
    project_id = demo["project"]
    diagnosis = client.post(
        f"/projects/{project_id}/diagnosis",
        headers=_headers(auth_headers, "diag-1"),
        json={
            "experience_level": "beginner",
            "weekly_hours": 6,
            "preferred_style": "practice",
        },
    )
    assert diagnosis.status_code == 201, diagnosis.text
    assert "初学" in diagnosis.json()["summary"]

    generated = client.post(
        f"/projects/{project_id}/plan/generate",
        headers=_headers(auth_headers, "generate-1"),
        json={},
    )
    assert generated.status_code == 201, generated.text
    payload = generated.json()
    assert payload["generator"] == "template/graph-v1"
    assert len(payload["milestones"]) == 3
    assert len(payload["tasks"]) == 3
    first_task = payload["tasks"][0]

    started = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/transition",
        headers=_headers(auth_headers, "start-1"),
        json={"expected_status": "pending", "next_status": "in_progress"},
    )
    assert started.status_code == 200, started.text

    submitted = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/submissions",
        headers=_headers(auth_headers, "submit-1"),
        json={"mode": "self_report", "content": "我能用自己的话解释核心概念。"},
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["evidence"]["component_id"] == "concept"

    before_done = client.get(
        f"/projects/{project_id}/mastery", headers=auth_headers()
    ).json()
    component = next(item for item in before_done["components"] if item["component_id"] == "concept")
    assert component["independence_level"] == "introduced"
    assert component["confidence"] == "low"

    done = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/transition",
        headers=_headers(auth_headers, "done-1"),
        json={"expected_status": "in_progress", "next_status": "done"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["verified"] is True

    after_done = client.get(
        f"/projects/{project_id}/mastery", headers=auth_headers()
    ).json()
    assert after_done == before_done


def test_plan_generation_requires_diagnosis(client, auth_headers, platform):
    actor_headers = auth_headers()
    project = client.post(
        "/projects",
        headers={**actor_headers, "Idempotency-Key": "new-project-no-diagnosis"},
        json={"name": "无诊断项目", "goal": "学数据库"},
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]

    response = client.post(
        f"/projects/{project_id}/plan/generate",
        headers={**actor_headers, "Idempotency-Key": "generate-without-diagnosis"},
        json={},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "PARAMS_INVALID"


def test_submission_endpoint_rejects_unmapped_manual_plan(
    client, auth_headers, demo
):
    project_id = demo["project"]
    plan = client.put(
        f"/projects/{project_id}/plan",
        headers=_headers(auth_headers, "manual-plan"),
        json={
            "goal": "手工计划",
            "milestones": [{"title": "阶段", "tasks": [{"title": "任务"}]}],
        },
    )
    task_id = plan.json()["tasks"][0]["task_id"]
    client.post(
        f"/projects/{project_id}/tasks/{task_id}/transition",
        headers=_headers(auth_headers, "manual-start"),
        json={"expected_status": "pending", "next_status": "in_progress"},
    )
    response = client.post(
        f"/projects/{project_id}/tasks/{task_id}/submissions",
        headers=_headers(auth_headers, "manual-submit"),
        json={"mode": "self_report", "content": "不能事后追认"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "EVIDENCE_UNMAPPED"
