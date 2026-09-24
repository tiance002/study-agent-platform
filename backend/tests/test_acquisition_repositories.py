"""Acquisition repository contract tests for the development adapter."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.core.clock import FixedClock
from app.core.errors import ErrorCode, PlatformError
from app.identity.membership import MembershipStore
from app.identity.models import Principal
from app.knowledge.acquisition import (
    AcquisitionRequest,
    CandidateStatus,
    DownloadStatus,
    SourceCandidate,
)
from app.knowledge.memory_acquisition_store import InMemoryAcquisitionRepository
from app.product.memory_store import InMemoryProductRepository

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
ALICE = Principal(principal_id="u_acq_alice", tenant_id="t_acq")
BOB = Principal(principal_id="u_acq_bob", tenant_id="t_acq")


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def setup_repo() -> tuple[MembershipStore, InMemoryProductRepository, InMemoryAcquisitionRepository, str, str]:
    membership = MembershipStore()
    products = InMemoryProductRepository(membership=membership)
    repo = InMemoryAcquisitionRepository(
        membership=membership,
        products=products,
        clock=FixedClock(NOW),
    )
    project_id = unique("proj")
    membership.create_project_for(ALICE, project_id=project_id, name="资料项目", goal="")
    source = products.register_source(
        ALICE,
        project_id,
        source_id=unique("src"),
        display_name="候选资料",
        media_type="text/plain",
        identity_hash="sha256:" + uuid.uuid4().hex,
        acquisition={"kind": "web"},
    )
    return membership, products, repo, project_id, source.source_id


def candidate(project_id: str, candidate_id: str | None = None) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=candidate_id or unique("cand"),
        tenant_id=ALICE.tenant_id,
        project_id=project_id,
        url="https://example.com/guide",
        title="Guide",
        snippet="A guide.",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def request(project_id: str, source_id: str, candidate_id: str, key: str) -> AcquisitionRequest:
    return AcquisitionRequest(
        acquisition_id=unique("acq"),
        tenant_id=ALICE.tenant_id,
        project_id=project_id,
        source_id=source_id,
        candidate_id=candidate_id,
        requested_by=ALICE.principal_id,
        url="https://example.com/guide",
        title="Guide",
        media_type="text/plain",
        language="en",
        idempotency_key=key,
        requested_at=NOW,
    )


def test_select_is_explicit_idempotent_and_marks_candidate_selected() -> None:
    _membership, _products, repo, project_id, source_id = setup_repo()
    item = candidate(project_id)
    repo.save_candidate(item)
    selected = repo.select(
        ALICE,
        project_id,
        request(project_id, source_id, item.candidate_id, "download-1"),
    )
    replay = repo.select(
        ALICE,
        project_id,
        request(project_id, source_id, item.candidate_id, "download-1"),
    )

    assert selected.acquisition_id == replay.acquisition_id
    assert selected.status is DownloadStatus.QUEUED
    assert repo.get_candidate(ALICE, project_id, item.candidate_id).status is CandidateStatus.SELECTED


def test_expired_selection_does_not_register_source_or_job() -> None:
    _membership, products, repo, project_id, _source_id = setup_repo()
    item = candidate(project_id)
    repo.save_candidate(item)
    repo.clock = FixedClock(item.expires_at)
    before = products.list_sources(ALICE, project_id)
    source_id = unique("src")

    with pytest.raises(PlatformError) as caught:
        repo.select_with_source(
            ALICE,
            project_id,
            request(project_id, source_id, item.candidate_id, "expired-selection"),
            source_display_name="过期资料",
            source_identity_hash="sha256:" + uuid.uuid4().hex,
            source_acquisition={"kind": "web", "url": item.url},
        )

    assert caught.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert products.list_sources(ALICE, project_id) == before
    assert repo.get_candidate(ALICE, project_id, item.candidate_id).status is CandidateStatus.DISCOVERED


def test_selection_rejects_candidate_from_another_project() -> None:
    membership, products, repo, project_a, source_a = setup_repo()
    project_b = unique("proj")
    membership.create_project_for(BOB, project_id=project_b, name="另一个项目", goal="")
    source_b = products.register_source(
        BOB,
        project_b,
        source_id=unique("src"),
        display_name="另一个来源",
        media_type="text/plain",
        identity_hash="sha256:" + uuid.uuid4().hex,
        acquisition={"kind": "web"},
    )
    foreign = candidate(project_b)
    repo.save_candidate(foreign)

    with pytest.raises(PlatformError) as caught:
        repo.select(
            ALICE,
            project_a,
            request(project_a, source_a, foreign.candidate_id, "download-foreign"),
        )
    assert caught.value.code is ErrorCode.CROSS_TENANT_DENIED
    assert source_b.source_id != source_a


def test_claim_is_fenced_and_unknown_is_not_claimed_again() -> None:
    _membership, _products, repo, project_id, source_id = setup_repo()
    item = candidate(project_id)
    repo.save_candidate(item)
    queued = repo.select(
        ALICE,
        project_id,
        request(project_id, source_id, item.candidate_id, "download-2"),
    )
    claimed = repo.claim_next(worker_id="acq-worker-a", lease_seconds=300)
    assert claimed is not None
    assert claimed.acquisition_id == queued.acquisition_id
    assert repo.claim_next(worker_id="acq-worker-b", lease_seconds=300) is None

    with pytest.raises(PlatformError) as caught:
        repo.settle(
            claimed,
            status=DownloadStatus.UNKNOWN,
            error_code="FETCH_TIMEOUT",
            safe_detail="来源请求超时",
            claim_token="stale-token",
        )
    assert caught.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION

    repo.settle(
        claimed,
        status=DownloadStatus.UNKNOWN,
        error_code="FETCH_TIMEOUT",
        safe_detail="来源请求超时",
        claim_token=claimed.claim_token,
    )
    assert repo.claim_next(worker_id="acq-worker-b", lease_seconds=300) is None
    assert repo.get_job(ALICE, project_id, claimed.acquisition_id).status is DownloadStatus.UNKNOWN
