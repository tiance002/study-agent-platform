"""Deterministic acquisition worker tests; no public network is used."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, DownloadStatus, SourceCandidate
from app.knowledge.fetch_policy import FetchPolicyError, FetchTarget
from app.knowledge.fetcher import FetchResult
from app.main import DEMO_PRINCIPAL, DEMO_PROJECT, DEMO_TENANT
from app.workers.acquisition import Outcome, run_once

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
ACTOR = Principal(principal_id=DEMO_PRINCIPAL, tenant_id=DEMO_TENANT)


def _queue(platform, *, key: str = "worker-key"):
    candidate = SourceCandidate(
        candidate_id=f"cand_{key}",
        tenant_id=DEMO_TENANT,
        project_id=DEMO_PROJECT,
        url="https://example.com/guide",
        title="事务指南",
        snippet="正文",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    platform.acquisition.create_candidate(ACTOR, DEMO_PROJECT, candidate)
    source = platform.products.register_source(
        ACTOR,
        DEMO_PROJECT,
        source_id=f"src_{key}",
        display_name=candidate.title,
        media_type="text/markdown",
        identity_hash=f"sha256:{key}",
        acquisition={"kind": "web", "url": candidate.url},
    )
    return platform.acquisition.select(
        ACTOR,
        DEMO_PROJECT,
        AcquisitionRequest(
            acquisition_id=f"acq_{key}",
            tenant_id=DEMO_TENANT,
            project_id=DEMO_PROJECT,
            source_id=source.source_id,
            candidate_id=candidate.candidate_id,
            requested_by=DEMO_PRINCIPAL,
            url=candidate.url,
            title=candidate.title,
            media_type="text/markdown",
            language="zh",
            idempotency_key=key,
            requested_at=NOW,
        ),
    )


def _result(content: bytes | None = None) -> FetchResult:
    if content is None:
        content = "# 事务\n\n提交成功\n".encode("utf-8")
    return FetchResult(
        final_target=FetchTarget(
            url="https://example.com/guide",
            hostname="example.com",
            port=443,
            resolved_ips=("93.184.216.34",),
        ),
        content=content,
        content_type="text/markdown; charset=utf-8",
        redirects=(),
    )


def test_worker_fetches_and_enqueues_ingestion(platform):
    queued = _queue(platform)

    outcome = run_once(
        platform,
        worker_id="acquisition-worker",
        fetcher=lambda _url: _result(),
    )

    assert outcome == Outcome(kind="succeeded", acquisition_id=queued.acquisition_id)
    stored = platform.acquisition.get_job(ACTOR, DEMO_PROJECT, queued.acquisition_id)
    assert stored.status is DownloadStatus.SUCCEEDED
    ingestion_jobs = platform.ingestion.list_jobs(ACTOR, DEMO_PROJECT)
    assert len(ingestion_jobs) == 1
    assert ingestion_jobs[0].source_id == queued.source_id
    document = platform.ingestion.load_document(ingestion_jobs[0])
    assert document.acquisition_method == "web_fetch"
    assert str(document.taint_sources[0]) == "web"


def test_retryable_network_error_becomes_unknown_without_requeue(platform):
    queued = _queue(platform, key="timeout")

    def timeout(_url):
        raise FetchPolicyError("FETCH_TIMEOUT", "来源请求超时", retryable=True)

    outcome = run_once(platform, worker_id="acquisition-worker", fetcher=timeout)

    assert outcome == Outcome(
        kind="unknown", acquisition_id=queued.acquisition_id, error_code="FETCH_TIMEOUT"
    )
    stored = platform.acquisition.get_job(ACTOR, DEMO_PROJECT, queued.acquisition_id)
    assert stored.status is DownloadStatus.UNKNOWN
    assert platform.acquisition.claim_next(worker_id="second", lease_seconds=60) is None


def test_deterministic_fetch_rejection_becomes_failed(platform):
    queued = _queue(platform, key="blocked")

    def blocked(_url):
        raise FetchPolicyError("FETCH_PAYLOAD_TOO_LARGE", "来源响应超过大小上限")

    outcome = run_once(platform, worker_id="acquisition-worker", fetcher=blocked)

    assert outcome == Outcome(
        kind="failed", acquisition_id=queued.acquisition_id, error_code="FETCH_PAYLOAD_TOO_LARGE"
    )
    assert platform.acquisition.get_job(ACTOR, DEMO_PROJECT, queued.acquisition_id).status is DownloadStatus.FAILED
