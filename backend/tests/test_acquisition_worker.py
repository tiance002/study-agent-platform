"""Deterministic acquisition worker tests; no public network is used."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from app.core.clock import FixedClock
from app.identity.models import Principal
from app.knowledge.acquisition import (
    AcquisitionRequest,
    DownloadStatus,
    SourceCandidate,
)
from app.knowledge.fetch_policy import FetchPolicyError, FetchTarget
from app.knowledge.fetcher import FetchResult
from app.main import DEMO_PRINCIPAL, DEMO_PROJECT, DEMO_TENANT
from app.workers.acquisition import Outcome, run_once
from app.workers.ingestion import run_once as ingest_once

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
ACTOR = Principal(principal_id=DEMO_PRINCIPAL, tenant_id=DEMO_TENANT)


def _queue(platform, *, key: str = "worker-key", source_identity_hash: str | None = None):
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
        identity_hash=source_identity_hash or f"sha256:{key}",
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


def _result(
    content: bytes | None = None,
    *,
    content_type: str = "text/markdown; charset=utf-8",
) -> FetchResult:
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
        content_type=content_type,
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


def test_same_source_content_and_parser_reuses_existing_document(platform):
    first = _queue(platform, key="same-content-first", source_identity_hash="sha256:same-url")
    second = _queue(platform, key="same-content-retry", source_identity_hash="sha256:same-url")

    assert first.source_id == second.source_id
    result = _result()
    run_once(platform, worker_id="first", fetcher=lambda _url: result)
    run_once(platform, worker_id="second", fetcher=lambda _url: result)

    jobs = platform.ingestion.list_jobs(ACTOR, DEMO_PROJECT)
    assert len(jobs) == 1
    document = platform.ingestion.load_document(jobs[0])
    assert document.version == 1
    assert document.fetch_attempt_id == first.acquisition_id


def test_changed_content_for_same_source_creates_new_document_version(platform):
    first = _queue(platform, key="changed-first", source_identity_hash="sha256:changed-url")
    second = _queue(platform, key="changed-second", source_identity_hash="sha256:changed-url")
    run_once(platform, worker_id="first", fetcher=lambda _url: _result(b"old content"))
    run_once(platform, worker_id="second", fetcher=lambda _url: _result(b"new content"))

    jobs = platform.ingestion.list_jobs(ACTOR, DEMO_PROJECT)
    documents = [platform.ingestion.load_document(job) for job in jobs]
    assert sorted(document.version for document in documents) == [1, 2]
    assert {document.content for document in documents} == {"old content", "new content"}
    assert {document.fetch_attempt_id for document in documents} == {
        first.acquisition_id,
        second.acquisition_id,
    }


def test_failed_ingestion_fingerprint_can_be_acquired_again(platform):
    first = _queue(platform, key="failed-version-first", source_identity_hash="sha256:failed-url")
    second = _queue(platform, key="failed-version-second", source_identity_hash="sha256:failed-url")
    run_once(platform, worker_id="first", fetcher=lambda _url: _result())
    failed_job = platform.ingestion.claim_next(worker_id="ingestion", lease_seconds=60)
    assert failed_job is not None
    platform.ingestion.fail(failed_job, error_code="TEST_FAILURE", safe_detail="测试解析失败")

    run_once(platform, worker_id="second", fetcher=lambda _url: _result())

    jobs = platform.ingestion.list_jobs(ACTOR, DEMO_PROJECT)
    documents = [platform.ingestion.load_document(job) for job in jobs]
    assert sorted(document.version for document in documents) == [1, 2]
    assert {document.fetch_attempt_id for document in documents} == {
        first.acquisition_id,
        second.acquisition_id,
    }


def test_worker_persists_raw_html_and_ingests_canonical_markdown(platform):
    queued = _queue(platform, key="html")
    raw_html = (
        "<article><h1>事务指南</h1><p>事务必须保持原子性。</p>"
        "<nav>导航噪声</nav><script>恶意文本</script></article>"
    ).encode("utf-8")

    outcome = run_once(
        platform,
        worker_id="acquisition-worker",
        fetcher=lambda _url: _result(raw_html, content_type="text/html; charset=utf-8"),
    )

    assert outcome.kind == "succeeded"
    ingestion_job = platform.ingestion.list_jobs(ACTOR, DEMO_PROJECT)[0]
    document = platform.ingestion.load_document(ingestion_job)
    artifact = platform.acquisition.load_artifact(queued)
    assert document.content == "# 事务指南\n\n事务必须保持原子性。"
    assert document.fetch_attempt_id == queued.acquisition_id
    assert document.source_content_type == "text/html"
    assert document.raw_content_hash == artifact.content_hash
    assert artifact.raw_content == raw_html
    assert "导航噪声" not in document.content
    assert "恶意文本" not in document.content


def test_html_acquisition_reaches_keyword_search_and_exact_citation_readback(platform):
    queued = _queue(platform, key="html-citation")
    raw_html = (
        "<article><h1>量子纠缠</h1><p>纠缠态的测量结果存在关联。</p></article>"
    ).encode("utf-8")
    acquired = run_once(
        platform,
        worker_id="acquisition-worker",
        fetcher=lambda _url: _result(raw_html, content_type="text/html"),
    )
    processed = ingest_once(platform, worker_id="ingestion-worker")

    assert acquired.kind == "succeeded"
    assert processed.kind == "succeeded"
    matches = platform.knowledge.search(ACTOR, DEMO_PROJECT, "量子纠缠测量")
    assert matches
    chunk = matches[0].chunk
    assert chunk.document_id == f"doc_web_{queued.acquisition_id}"
    assert "纠缠态的测量结果存在关联。" in chunk.content
    readback = platform.knowledge.read_span(
        ACTOR,
        DEMO_PROJECT,
        chunk.source_id,
        chunk.span,
        document_id=chunk.document_id,
        content_hash=chunk.content_hash,
    )
    assert readback == chunk


def test_worker_reuses_saved_artifact_after_crash_without_refetching(platform):
    queued = _queue(platform, key="html-recovery")
    original_enqueue = platform.ingestion.enqueue
    fetch_calls = 0

    def fetch(_url):
        nonlocal fetch_calls
        fetch_calls += 1
        return _result(
            "<article><h1>恢复</h1><p>使用已保存响应。</p></article>".encode("utf-8"),
            content_type="text/html",
        )

    def crash_after_artifact(*_args, **_kwargs):
        raise RuntimeError("simulated process crash after artifact persistence")

    platform.ingestion.enqueue = crash_after_artifact
    with pytest.raises(RuntimeError, match="simulated process crash"):
        run_once(
            platform,
            worker_id="first-worker",
            lease_seconds=1,
            fetcher=fetch,
        )

    assert platform.acquisition.load_artifact(queued) is not None
    platform.ingestion.enqueue = original_enqueue
    time.sleep(1.05)
    outcome = run_once(
        platform,
        worker_id="recovery-worker",
        fetcher=lambda _url: pytest.fail("saved acquisition must not be fetched twice"),
    )

    assert outcome.kind == "succeeded"
    assert fetch_calls == 1


def test_expired_acquisition_claims_stop_at_the_attempt_limit(platform):
    clock = FixedClock(NOW)
    platform.acquisition.clock = clock
    queued = _queue(platform, key="crash-limit")

    for attempt in range(3):
        claimed = platform.acquisition.claim_next(worker_id=f"crashed-{attempt}", lease_seconds=1)
        assert claimed is not None
        assert claimed.attempt_count == attempt + 1
        clock.advance(seconds=2)

    assert platform.acquisition.claim_next(worker_id="after-limit", lease_seconds=1) is None
    failed = platform.acquisition.get_job(ACTOR, DEMO_PROJECT, queued.acquisition_id)
    assert failed.status is DownloadStatus.UNKNOWN
    assert failed.error_code == "ACQUISITION_ATTEMPTS_EXHAUSTED"
    assert failed.attempt_count == 3


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
    assert (
        platform.acquisition.get_job(ACTOR, DEMO_PROJECT, queued.acquisition_id).status
        is DownloadStatus.FAILED
    )
