"""PostgreSQL acquisition job persistence and worker fencing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pg_support
import psycopg
import pytest
from app.db.acquisition_store import PostgresAcquisitionRepository
from app.db.identity_store import PostgresMembershipRepository
from app.db.ingestion_store import PostgresIngestionRepository
from app.db.product_store import PostgresProductRepository
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, DownloadStatus, SourceCandidate
from app.knowledge.fetch_policy import FetchTarget
from app.knowledge.fetcher import FetchResult
from app.workers.acquisition import Outcome, run_once

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not pg_support.reachable(), reason="本地 PostgreSQL 未运行"),
]

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TENANT = "t_acq_pg"
ALICE = "u_acq_pg_alice"


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


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


def test_postgres_worker_persists_web_content_before_settling(pg_env) -> None:
    actor, repo, membership, _products, project_id, source_id = pg_env
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
            source_id=source_id,
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
            content=b"# Worker\n\nPersisted.\n",
            content_type="text/markdown",
            redirects=(),
        ),
    )

    assert outcome == Outcome(kind="succeeded", acquisition_id=queued.acquisition_id)
    assert repo.get_job(actor, project_id, queued.acquisition_id).status is DownloadStatus.SUCCEEDED
    ingestion_job = ingestion.list_jobs(actor, project_id)[0]
    document = ingestion.load_document(ingestion_job)
    assert document.acquisition_method == "web_fetch"
    assert str(document.taint_sources[0]) == "web"
