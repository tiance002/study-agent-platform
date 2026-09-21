"""第八轮真实 HTTP 协议回归。"""

from __future__ import annotations

import uuid


def test_empty_json_endpoints_return_a_truly_empty_204_body(cookie_project):
    client, project_id = cookie_project

    plan = client.get(f"/projects/{project_id}/plan")
    diagnosis = client.get(f"/projects/{project_id}/diagnosis")

    assert plan.status_code == 204
    assert plan.content == b""
    assert diagnosis.status_code == 204
    assert diagnosis.content == b""


def test_project_ingestion_job_list_exposes_durable_status_metadata(cookie_project):
    client, project_id = cookie_project
    headers = {"Origin": "http://testserver", "Idempotency-Key": "r8-" + uuid.uuid4().hex}
    source = client.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "状态资料", "acquisition": {"kind": "round8"}},
        headers=headers,
    )
    assert source.status_code == 201
    source_id = source.json()["source_id"]
    upload = client.post(
        f"/projects/{project_id}/sources/{source_id}/content",
        json={"title": "状态资料", "content": "可回读正文。", "media_type": "text/plain", "language": "zh"},
        headers={**headers, "Idempotency-Key": "r8-content-" + uuid.uuid4().hex},
    )
    assert upload.status_code == 202
    job_id = upload.json()["job"]["job_id"]

    listed = client.get(f"/projects/{project_id}/ingestion-jobs")

    assert listed.status_code == 200
    assert listed.json()["jobs"][0]["job_id"] == job_id
    assert listed.json()["jobs"][0]["source_id"] == source_id
    assert listed.json()["jobs"][0]["status"] == "queued"
