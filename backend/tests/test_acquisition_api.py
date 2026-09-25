"""HTTP contract for explicit candidate creation and acquisition selection."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from app.core.clock import FixedClock
from app.knowledge.discovery import SearchResult

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


def test_expired_candidate_selection_does_not_leave_orphan_source(acquisition_project, platform):
    client, project_id = acquisition_project
    candidate = client.post(
        f"/projects/{project_id}/source-candidates",
        json={"url": "https://example.com/expired", "title": "过期候选"},
        headers=keyed("expired-candidate"),
    ).json()
    platform.acquisition.clock = FixedClock(datetime.fromisoformat(candidate["expires_at"]))

    response = client.post(
        f"/projects/{project_id}/source-candidates/{candidate['candidate_id']}/select",
        json={"display_name": "过期候选", "media_type": "text/plain", "language": "zh"},
        headers=keyed("expired-select"),
    )

    assert response.status_code == 403
    assert client.get(f"/projects/{project_id}/sources").json()["sources"] == []


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


def test_search_results_are_persisted_as_unselected_candidates(acquisition_project, platform):
    client, project_id = acquisition_project

    class SearchProvider:
        calls = 0

        def search(self, query: str, *, limit: int):
            self.calls += 1
            assert query == "事务隔离"
            assert limit == 5
            return (
                SearchResult("指南", "https://example.com/guide", "公开摘要"),
                SearchResult("私网", "http://127.0.0.1/private", "不得入库"),
                SearchResult("重复", "https://example.com/guide", "重复网址"),
            )

    provider = SearchProvider()
    platform.source_search_provider = provider
    headers = keyed("source-search-once")
    response = client.post(
        f"/projects/{project_id}/source-search",
        json={"query": "事务隔离", "limit": 5},
        headers=headers,
    )

    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["status"] == "discovered"
    assert candidates[0]["snippet"] == "公开摘要"
    replay = client.post(
        f"/projects/{project_id}/source-search",
        json={"query": "事务隔离", "limit": 5},
        headers=headers,
    )
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == response.json()
    assert provider.calls == 1


def test_source_search_is_explicitly_disabled_without_provider(acquisition_project):
    client, project_id = acquisition_project
    response = client.post(
        f"/projects/{project_id}/source-search",
        json={"query": "事务隔离"},
        headers=keyed("disabled-search"),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "SOURCE_SEARCH_DISABLED"


def test_search_provider_error_is_cached_to_prevent_duplicate_dispatch(
    acquisition_project, platform
):
    client, project_id = acquisition_project

    class BrokenProvider:
        calls = 0

        def search(self, _query: str, *, limit: int):
            self.calls += 1
            raise RuntimeError("provider body must stay private")

    provider = BrokenProvider()
    platform.source_search_provider = provider
    headers = keyed("failed-source-search")
    path = f"/projects/{project_id}/source-search"
    body = {"query": "事务隔离"}
    first = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)

    assert first.status_code == replay.status_code == 503
    assert first.json()["code"] == "SOURCE_SEARCH_UNAVAILABLE"
    assert "provider body" not in first.text
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert provider.calls == 1

    fresh_attempt = client.post(path, json=body, headers=keyed("fresh-search-attempt"))
    assert fresh_attempt.status_code == 503
    assert provider.calls == 2


def test_source_search_is_persistently_rate_limited_before_provider_dispatch(
    acquisition_project, platform
):
    client, project_id = acquisition_project

    class SearchProvider:
        calls = 0

        def search(self, _query: str, *, limit: int):
            self.calls += 1
            return ()

    provider = SearchProvider()
    platform.source_search_provider = provider
    path = f"/projects/{project_id}/source-search"
    body = {"query": "事务隔离"}

    for index in range(20):
        response = client.post(path, json=body, headers=keyed(f"search-{index}"))
        assert response.status_code == 200

    blocked = client.post(path, json=body, headers=keyed("search-over-limit"))

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) > 0
    assert provider.calls == 20

    replay = client.post(path, json=body, headers=keyed("search-0"))
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert provider.calls == 20
