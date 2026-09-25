# -*- coding: utf-8 -*-
"""第 3 轮用户路径：诊断 -> 生成计划 -> 提交 -> 掌握投影。

A03 之后的关键语义：**自报 ≠ 已验证**。提交自报会产生一条中性的学习证据
（落库保留审计），但它既不会把任务 `verified` 置为 True，也不会抬升掌握度；
前端把 `verified=false + self_reported=true` 显示为"自报反馈已记录"。
"""

from __future__ import annotations


def _headers(auth_headers, key: str) -> dict[str, str]:
    return {**auth_headers(), "Idempotency-Key": key}


def test_learning_loop_records_self_report_without_forging_verified(
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
    assert payload["generator"] == "template/graph-v2"
    assert len(payload["milestones"]) == 3
    assert len(payload["tasks"]) == 6
    first_task = payload["tasks"][0]
    # 分解出来的任务带完整详情，而不只是一行标题。
    assert first_task["objective"]
    assert first_task["instruction"]
    assert first_task["deliverable"]
    assert first_task["acceptance_criteria"]
    assert first_task["estimated_minutes"] > 0
    assert first_task["task_type"] in {"concept", "practice", "reflection"}

    detail_before = client.get(
        f"/projects/{project_id}/tasks/{first_task['task_id']}", headers=auth_headers()
    ).json()
    assert detail_before["verified"] is False
    assert detail_before["self_reported"] is False

    started = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/transition",
        headers=_headers(auth_headers, "start-1"),
        json={"expected_status": "pending", "next_status": "in_progress"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["self_reported"] is False

    submitted = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/submissions",
        headers=_headers(auth_headers, "submit-1"),
        json={"mode": "self_report", "content": "我能用自己的话解释核心概念。"},
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["evidence"]["component_id"] == first_task["task_type"]

    # 自报之后：self_reported 为真，但 verified 仍然为假（自报不是评分）。
    detail_after = client.get(
        f"/projects/{project_id}/tasks/{first_task['task_id']}", headers=auth_headers()
    ).json()
    assert detail_after["self_reported"] is True
    assert detail_after["verified"] is False

    mastery = client.get(f"/projects/{project_id}/mastery", headers=auth_headers()).json()
    component = next(
        item for item in mastery["components"] if item["component_id"] == first_task["task_type"]
    )
    # 中性裁决不抬升独立水平：证据被记录（count=1），但水平仍是 unknown。
    assert component["independence_level"] == "unknown"
    assert component["evidence_count"] == 1

    done = client.post(
        f"/projects/{project_id}/tasks/{first_task['task_id']}/transition",
        headers=_headers(auth_headers, "done-1"),
        json={"expected_status": "in_progress", "next_status": "done"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["verified"] is False
    assert done.json()["self_reported"] is True

    after_done = client.get(f"/projects/{project_id}/mastery", headers=auth_headers()).json()
    assert after_done == mastery


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
