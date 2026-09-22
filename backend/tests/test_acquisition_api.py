"""HTTP contract for explicit candidate creation and acquisition selection."""

from __future__ import annotations

import uuid

import pytest

ORIGIN = {"Origin": "http://testserver"}


def keyed(value: str | None = None) -> dict[str, str]:
    return {**ORIGIN, "Idempotency-Key": value or "acq-" + uuid.uuid4().hex}


@pytest.fixture
def acquisition_project(cookie_project):
    client, project_id = cookie_project
    return client, project_id


def test_user_can_create_candidate_select_it_and_read_durable_job(acquisition_project):
    client, project_id = acquisition_project
    candidate_response = client.post(
        f"/projects/{project_id}/source-candidates",
        json={
            "url": "https://example.com/guide",
            "title": "事务指南",
            "snippet": "一份公开指南",
        },
        headers=keyed("candidate-1"),
    )

    assert candidate_response.status_code == 201
    candidate = candidate_response.json()
    assert candidate["status"] == "discovered"
    assert candidate["source_domain"] == "example.com"

    listed = client.get(f"/projects/{project_id}/source-candidates")
    assert listed.status_code == 200
    assert [row["candidate_id"] for row in listed.json()["candidates"]] == [
        candidate["candidate_id"]
    ]

    selected = client.post(
        f"/projects/{project_id}/source-candidates/{candidate['candidate_id']}/select",
        json={
            "display_name": "事务指南",
            "media_type": "text/markdown",
            "language": "zh",
        },
        headers=keyed("select-1"),
    )

    assert selected.status_code == 202
    payload = selected.json()
    assert payload["candidate"]["status"] == "selected"
    assert payload["source"]["source_id"]
    assert payload["acquisition"]["status"] == "queued"
    assert payload["acquisition"]["url"] == "https://example.com/guide"

    replay = client.post(
        f"/projects/{project_id}/source-candidates/{candidate['candidate_id']}/select",
        json={
            "display_name": "事务指南",
            "media_type": "text/markdown",
            "language": "zh",
        },
        headers=keyed("select-1"),
    )
    assert replay.status_code == 202
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == payload

    job_id = payload["acquisition"]["acquisition_id"]
    status = client.get(f"/projects/{project_id}/acquisition-jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert "error_detail" in status.json()


def test_candidate_creation_rejects_private_target_before_persistence(acquisition_project):
    client, project_id = acquisition_project
    response = client.post(
        f"/projects/{project_id}/source-candidates",
        json={
            "url": "http://127.0.0.1/secret",
            "title": "内网地址",
        },
        headers=keyed(),
    )
    assert response.status_code == 422
    assert client.get(f"/projects/{project_id}/source-candidates").json()["candidates"] == []


def test_candidate_from_another_project_is_not_selectable(acquisition_project):
    client, project_id = acquisition_project
    other = client.post(
        "/projects",
        json={"name": "另一个项目"},
        headers=keyed("other-project"),
    )
    other_id = other.json()["project_id"]
    created = client.post(
        f"/projects/{other_id}/source-candidates",
        json={"url": "https://example.com/other", "title": "其它"},
        headers=keyed("other-candidate"),
    )
    candidate_id = created.json()["candidate_id"]

    response = client.post(
        f"/projects/{project_id}/source-candidates/{candidate_id}/select",
        json={"display_name": "越权", "media_type": "text/plain", "language": "zh"},
        headers=keyed(),
    )
    assert response.status_code == 404
