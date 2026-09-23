"""PostgreSQL acquisition job persistence and worker fencing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pg_support
import psycopg
import pytest
from app.core.clock import SystemClock
from app.core.ids import new_id
from app.db.acquisition_store import PostgresAcquisitionRepository
from app.db.identity_store import PostgresMembershipRepository
from app.db.ingestion_store import PostgresIngestionRepository
from app.db.product_store import PostgresProductRepository
from app.db.rate_limit_store import PostgresRateLimiter
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, DownloadStatus, SourceCandidate
from app.knowledge.fetch_policy import FetchTarget
from app.knowledge.fetcher import FetchResult
from app.knowledge.store import KnowledgeRepository
from app.workers.acquisition import Outcome, run_once
from app.workers.ingestion import run_once as ingest_once

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not pg_support.reachable(), reason="本地 PostgreSQL 未运行"),
]

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TENANT = "t_acq_pg"
ALICE = "u_acq_pg_alice"


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_postgres_source_search_limit_survives_limiter_reconstruction() -> None:
    key = unique("source_search")
    now = datetime.now(timezone.utc)
    first = PostgresRateLimiter(limit=1, window_seconds=1)

    allowed = first.register(key, now=now, limit=20, window_seconds=600)
    assert allowed.allowed
    assert allowed.attempts == 1

    reconstructed = PostgresRateLimiter(limit=1, window_seconds=1)
    decisions = [reconstructed.register(key, now=now, limit=20, window_seconds=600) for _ in range(20)]
    assert all(decision.allowed for decision in decisions[:-1])
    assert decisions[-1].blocked
    assert decisions[-1].attempts == 21
    assert 0 < decisions[-1].retry_after_seconds <= 600


@pytest.fixture(scope="module")
def pg_seed() -> None:
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (TENANT, TENANT),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (ALICE, TENANT),
            )


@pytest.fixture
def pg_env(pg_seed):
    actor = Principal(principal_id=ALICE, tenant_id=TENANT)
    membership = PostgresMembershipRepository()
    products = PostgresProductRepository(membership=membership)
    repo = PostgresAcquisitionRepository(membership=membership, products=products)
    project_id = unique("proj")
    membership.create_project_for(actor, project_id=project_id, name="资料项目", goal="")
    source = products.register_source(
        actor,
        project_id,
        source_id=unique("src"),
        display_name="候选资料",
        media_type="text/plain",
        identity_hash="sha256:" + uuid.uuid4().hex,
        acquisition={"kind": "web"},
    )
    return actor, repo, membership, products, project_id, source.source_id


def test_postgres_selection_survives_repository_reconstruction(pg_env) -> None:
    actor, repo, membership, products, project_id, source_id = pg_env
    candidate_id = unique("cand")
    candidate = SourceCandidate(
        candidate_id=candidate_id,
        tenant_id=actor.tenant_id,
        project_id=project_id,
        url="https://example.com/guide",
        title="Guide",
        snippet="A guide.",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    repo.save_candidate(candidate)
    request = AcquisitionRequest(
        acquisition_id=unique("acq"),
        tenant_id=actor.tenant_id,
        project_id=project_id,
        source_id=source_id,
        candidate_id=candidate_id,
        requested_by=actor.principal_id,
        url=candidate.url,
        title=candidate.title,
        media_type="text/plain",
        language="en",
        idempotency_key=unique("key"),
        requested_at=NOW,
    )
    queued = repo.select(actor, project_id, request)
    same = repo.select(actor, project_id, request)
    assert same.acquisition_id == queued.acquisition_id
    reconstructed = PostgresAcquisitionRepository(membership=membership, products=products)
    assert reconstructed.get_job(actor, project_id, queued.acquisition_id).status is DownloadStatus.QUEUED
    claimed = repo.claim_next(worker_id=unique("worker"), lease_seconds=300)
    assert claimed is not None and claimed.acquisition_id == queued.acquisition_id
    repo.settle(
        claimed,
        status=DownloadStatus.UNKNOWN,
        error_code="TEST_CLEANUP",
        safe_detail="测试清理",
        claim_token=claimed.claim_token,
    )


def test_postgres_app_role_can_create_and_list_candidates(pg_env) -> None:
    actor, repo, _membership, _products, project_id, _source_id = pg_env
    candidate = SourceCandidate(
        candidate_id=unique("cand"),
        tenant_id=actor.tenant_id,
        project_id=project_id,
        url="https://example.com/app-created",
        title="App candidate",
        snippet="created by the web role",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert repo.create_candidate(actor, project_id, candidate) == candidate
    assert repo.list_candidates(actor, project_id) == (candidate,)


def test_postgres_worker_claim_and_unknown_settlement_are_fenced(pg_env) -> None:
    actor, repo, _membership, _products, project_id, source_id = pg_env
    candidate_id = unique("cand")
    repo.save_candidate(
        SourceCandidate(
            candidate_id=candidate_id,
            tenant_id=actor.tenant_id,
            project_id=project_id,
            url="https://example.com/guide",
            title="Guide",
            snippet="A guide.",
            source_domain="example.com",
            discovered_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    queued = repo.select(
        actor,
        project_id,
        AcquisitionRequest(
            acquisition_id=unique("acq"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source_id,
            candidate_id=candidate_id,
            requested_by=actor.principal_id,
            url="https://example.com/guide",
            title="Guide",
            media_type="text/plain",
            language="en",
            idempotency_key=unique("key"),
            requested_at=NOW,
        ),
    )
    claimed = repo.claim_next(worker_id=unique("worker"), lease_seconds=300)
    assert claimed is not None and claimed.acquisition_id == queued.acquisition_id
    repo.settle(
        claimed,
        status=DownloadStatus.UNKNOWN,
        error_code="FETCH_TIMEOUT",
        safe_detail="来源请求超时",
        claim_token=claimed.claim_token,
    )
    assert repo.get_job(actor, project_id, queued.acquisition_id).status is DownloadStatus.UNKNOWN


def test_postgres_expired_claims_stop_at_the_attempt_limit(pg_env) -> None:
    actor, repo, _membership, _products, project_id, source_id = pg_env
    candidate = SourceCandidate(
        candidate_id=unique("cand"),
        tenant_id=actor.tenant_id,
        project_id=project_id,
        url="https://example.com/crash-limit",
        title="Crash limit",
        snippet="",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    repo.create_candidate(actor, project_id, candidate)
    queued = repo.select(
        actor,
        project_id,
        AcquisitionRequest(
            acquisition_id=unique("acq"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source_id,
            candidate_id=candidate.candidate_id,
            requested_by=actor.principal_id,
            url=candidate.url,
            title=candidate.title,
            media_type="text/plain",
            language="en",
            idempotency_key=unique("key"),
            requested_at=NOW,
        ),
    )

    for attempt in range(3):
        claimed = repo.claim_next(worker_id=unique("crashed"), lease_seconds=300)
        assert claimed is not None
        assert claimed.acquisition_id == queued.acquisition_id
        assert claimed.attempt_count == attempt + 1
        with psycopg.connect(pg_support.migration_dsn()) as conn:
            conn.execute(
                "UPDATE acquisition_jobs SET lease_until = now() - interval '1 second'"
                " WHERE acquisition_id = %s",
                (queued.acquisition_id,),
            )

    assert repo.claim_next(worker_id=unique("after-limit"), lease_seconds=300) is None
    stopped = repo.get_job(actor, project_id, queued.acquisition_id)
    assert stopped.status is DownloadStatus.UNKNOWN
    assert stopped.attempt_count == 3
    assert stopped.error_code == "ACQUISITION_ATTEMPTS_EXHAUSTED"
    assert stopped.error_detail == "下载任务连续中断，已停止自动重试"


def test_postgres_worker_persists_web_content_before_settling(pg_env) -> None:
    actor, repo, membership, products, project_id, _source_id = pg_env
    source = products.register_source(
        actor,
        project_id,
        source_id=unique("src"),
        display_name="HTML worker guide",
        media_type="text/markdown",
        identity_hash="sha256:" + uuid.uuid4().hex,
        acquisition={"kind": "web"},
    )
    candidate = SourceCandidate(
        candidate_id=unique("cand"),
        tenant_id=actor.tenant_id,
        project_id=project_id,
        url="https://example.com/worker",
        title="Worker guide",
        snippet="正文",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    repo.create_candidate(actor, project_id, candidate)
    queued = repo.select(
        actor,
        project_id,
        AcquisitionRequest(
            acquisition_id=unique("acq"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source.source_id,
            candidate_id=candidate.candidate_id,
            requested_by=actor.principal_id,
            url=candidate.url,
            title=candidate.title,
            media_type="text/markdown",
            language="en",
            idempotency_key=unique("key"),
            requested_at=NOW,
        ),
    )
    ingestion = PostgresIngestionRepository(membership=membership)
    worker_platform = SimpleNamespace(acquisition=repo, ingestion=ingestion)

    outcome = run_once(
        worker_platform,
        worker_id=unique("worker"),
        fetcher=lambda _url: FetchResult(
            final_target=FetchTarget(
                url=candidate.url,
                hostname="example.com",
                port=443,
                resolved_ips=("93.184.216.34",),
            ),
            content="<article><h1>Worker</h1><p>Persisted.</p></article>".encode(),
            content_type="text/html; charset=utf-8",
            redirects=(),
        ),
    )

    assert outcome == Outcome(kind="succeeded", acquisition_id=queued.acquisition_id)
    assert repo.get_job(actor, project_id, queued.acquisition_id).status is DownloadStatus.SUCCEEDED
    ingestion_job = ingestion.list_jobs(actor, project_id)[0]
    document = ingestion.load_document(ingestion_job)
    assert document.acquisition_method == "web_fetch"
    assert str(document.taint_sources[0]) == "web"
    artifact = repo.load_artifact(queued)
    assert artifact is not None
    assert artifact.content_type == "text/html"
    assert artifact.raw_content.startswith(b"<article>")
    assert document.content == "# Worker\n\nPersisted."
    assert document.fetch_attempt_id == queued.acquisition_id
    assert document.source_content_type == artifact.content_type
    assert document.raw_content_hash == artifact.content_hash
    assert document.parser_version == "html-to-markdown/v1"
    assert ingest_once(
        SimpleNamespace(ingestion=ingestion, clock=SystemClock()),
        worker_id=new_id("ingestworker"),
    ).kind == "succeeded"
    knowledge = KnowledgeRepository(ingestion=ingestion)
    matches = knowledge.search(actor, project_id, "Worker Persisted")
    assert matches
    chunk = matches[0].chunk
    assert chunk.document_id == document.document_id
    assert knowledge.read_span(
        actor,
        project_id,
        chunk.source_id,
        chunk.span,
        document_id=chunk.document_id,
        content_hash=chunk.content_hash,
    ) == chunk

    with psycopg.connect(pg_support.app_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "SELECT raw_content FROM source_fetch_artifacts WHERE acquisition_id = %s",
                (queued.acquisition_id,),
            ).fetchone()
    with psycopg.connect(pg_support.worker_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE source_fetch_artifacts SET parser_version = 'web-text/v1' WHERE acquisition_id = %s",
                (queued.acquisition_id,),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "DELETE FROM source_fetch_artifacts WHERE acquisition_id = %s",
                (queued.acquisition_id,),
            )


def test_postgres_same_web_fingerprint_reuses_document_and_job(pg_env) -> None:
    actor, repo, membership, products, project_id, _ = pg_env
    identity_hash = "sha256:" + uuid.uuid4().hex
    source = products.register_source(
        actor,
        project_id,
        source_id=unique("src"),
        display_name="Repeat guide",
        media_type="text/plain",
        identity_hash=identity_hash,
        acquisition={"kind": "web", "url": "https://example.com/repeat"},
    )
    ingestion = PostgresIngestionRepository(membership=membership)
    worker_platform = SimpleNamespace(acquisition=repo, ingestion=ingestion)
    queued_jobs = []

    for suffix in ("one", "two"):
        candidate = SourceCandidate(
            candidate_id=unique("cand"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            url="https://example.com/repeat",
            title="Repeat guide",
            snippet="same content",
            source_domain="example.com",
            discovered_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        repo.create_candidate(actor, project_id, candidate)
        queued = repo.select(
            actor,
            project_id,
            AcquisitionRequest(
                acquisition_id=unique("acq"),
                tenant_id=actor.tenant_id,
                project_id=project_id,
                source_id=source.source_id,
                candidate_id=candidate.candidate_id,
                requested_by=actor.principal_id,
                url=candidate.url,
                title=candidate.title,
                media_type="text/plain",
                language="en",
                idempotency_key=unique("key"),
                requested_at=NOW,
            ),
        )
        queued_jobs.append(queued)
        result = FetchResult(
            final_target=FetchTarget(
                url=candidate.url,
                hostname="example.com",
                port=443,
                resolved_ips=("93.184.216.34",),
            ),
            content=b"identical web content",
            content_type="text/plain; charset=utf-8",
            redirects=(),
        )
        outcome = run_once(
            worker_platform,
            worker_id=f"{suffix}-{unique('worker')}",
            fetcher=lambda _url, fetched=result: fetched,
        )
        assert outcome == Outcome(kind="succeeded", acquisition_id=queued.acquisition_id)

    jobs = ingestion.list_jobs(actor, project_id)
    assert len(jobs) == 1
    document = ingestion.load_document(jobs[0])
    assert document.version == 1
    assert document.fetch_attempt_id == queued_jobs[0].acquisition_id
    assert all(
        repo.get_job(actor, project_id, item.acquisition_id).status is DownloadStatus.SUCCEEDED
        for item in queued_jobs
    )
