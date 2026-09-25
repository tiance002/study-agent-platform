"""Round 9 acquisition protocol tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from app.knowledge.acquisition import (
    AcquisitionJob,
    AcquisitionRequest,
    CandidateStatus,
    DownloadStatus,
    SourceCandidate,
    claim_download,
    transition_candidate,
    transition_download,
)
from app.knowledge.models import ACQUISITION_METHOD_WEB, SourceDocument

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def candidate() -> SourceCandidate:
    return SourceCandidate(
        candidate_id="cand_1",
        tenant_id="tenant_1",
        project_id="proj_1",
        url="https://example.com/guide",
        title="Guide",
        snippet="A short candidate description.",
        source_domain="example.com",
        discovered_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def job(status: DownloadStatus = DownloadStatus.QUEUED) -> AcquisitionJob:
    return AcquisitionJob(
        acquisition_id="acq_1",
        tenant_id="tenant_1",
        project_id="proj_1",
        source_id="src_1",
        candidate_id="cand_1",
        requested_by="user_1",
        url="https://example.com/guide",
        title="Guide",
        media_type="text/plain",
        language="en",
        idempotency_key="download-1",
        status=status,
        attempt_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def test_candidate_selection_is_explicit_and_terminal_rejection_is_stable() -> None:
    selected = transition_candidate(candidate(), CandidateStatus.SELECTED)
    assert selected.status is CandidateStatus.SELECTED
    assert transition_candidate(selected, CandidateStatus.REJECTED).status is CandidateStatus.REJECTED

    rejected = transition_candidate(selected, CandidateStatus.REJECTED)
    with pytest.raises(ValueError, match="candidate transition"):
        transition_candidate(rejected, CandidateStatus.EXPIRED)

    with pytest.raises(ValueError, match="source_domain"):
        SourceCandidate(
            **{
                "candidate_id": "cand_2",
                "tenant_id": "tenant_1",
                "project_id": "proj_1",
                "url": "https://example.com/guide",
                "title": "Guide",
                "snippet": "",
                "source_domain": "evil.example",
                "discovered_at": NOW,
                "expires_at": NOW + timedelta(hours=1),
            }
        )


def test_download_unknown_is_not_an_automatic_retry_path() -> None:
    running = claim_download(
        job(), lease_owner="worker_1", lease_until=NOW + timedelta(minutes=5), claim_token="token_1"
    )
    unknown = transition_download(running, DownloadStatus.UNKNOWN)
    assert unknown.status is DownloadStatus.UNKNOWN

    with pytest.raises(ValueError, match="download transition"):
        transition_download(unknown, DownloadStatus.QUEUED)


def test_download_success_and_failure_are_terminal() -> None:
    running = claim_download(
        job(), lease_owner="worker_1", lease_until=NOW + timedelta(minutes=5), claim_token="token_1"
    )
    assert transition_download(running, DownloadStatus.SUCCEEDED).status is DownloadStatus.SUCCEEDED
    assert transition_download(running, DownloadStatus.FAILED).status is DownloadStatus.FAILED


def test_acquisition_job_rejects_invalid_status_lease_combinations() -> None:
    with pytest.raises(ValueError, match="lease"):
        replace(job(), status=DownloadStatus.RUNNING)
    with pytest.raises(ValueError, match="重试上限"):
        replace(job(), attempt_count=4)


def test_selection_request_has_one_idempotent_queued_job_shape() -> None:
    request = AcquisitionRequest(
        acquisition_id="acq_2",
        tenant_id="tenant_1",
        project_id="proj_1",
        source_id="src_1",
        candidate_id="cand_1",
        requested_by="user_1",
        url="https://example.com/guide",
        title="Guide",
        media_type="text/plain",
        language="en",
        idempotency_key="download-1",
        requested_at=NOW,
    )
    queued = request.to_queued_job()
    assert queued.status is DownloadStatus.QUEUED
    assert queued.acquisition_id == request.acquisition_id
    assert queued.attempt_count == 0


def test_acquisition_rejects_media_type_without_a_completed_parser() -> None:
    with pytest.raises(ValueError, match="media_type"):
        AcquisitionRequest(
            acquisition_id="acq_html",
            tenant_id="tenant_1",
            project_id="proj_1",
            source_id="src_1",
            candidate_id="cand_1",
            requested_by="user_1",
            url="https://example.com/guide",
            title="Guide",
            media_type="text/html",
            language="en",
            idempotency_key="download-html",
            requested_at=NOW,
        )


def test_web_source_document_requires_fetch_provenance() -> None:
    with pytest.raises(ValueError, match="web provenance"):
        SourceDocument(
            document_id="doc_web",
            tenant_id="tenant_1",
            project_id="proj_1",
            source_id="src_1",
            version=1,
            document_title="Guide",
            content="# Guide",
            media_type="text/markdown",
            language="en",
            observed_at=NOW,
            acquisition_method=ACQUISITION_METHOD_WEB,
        )


def test_web_source_document_keeps_raw_fetch_fingerprint_and_parser_version() -> None:
    document = SourceDocument(
        document_id="doc_web",
        tenant_id="tenant_1",
        project_id="proj_1",
        source_id="src_1",
        version=1,
        document_title="Guide",
        content="# Guide",
        media_type="text/markdown",
        language="en",
        observed_at=NOW,
        parser_version="html-to-markdown/v1",
        acquisition_method=ACQUISITION_METHOD_WEB,
        fetch_attempt_id="acq_1",
        source_content_type="text/html",
        raw_content_hash="sha256:" + "a" * 64,
    )

    assert document.to_dict()["fetch_attempt_id"] == "acq_1"
    assert document.to_dict()["source_content_type"] == "text/html"
    assert document.to_dict()["raw_content_hash"] == "sha256:" + "a" * 64


def test_web_source_document_rejects_unknown_parser_version() -> None:
    with pytest.raises(ValueError, match="解析器版本"):
        SourceDocument(
            document_id="doc_web",
            tenant_id="tenant_1",
            project_id="proj_1",
            source_id="src_1",
            version=1,
            document_title="Guide",
            content="# Guide",
            media_type="text/markdown",
            language="en",
            observed_at=NOW,
            parser_version="unknown/v9",
            acquisition_method=ACQUISITION_METHOD_WEB,
            fetch_attempt_id="acq_1",
            source_content_type="text/html",
            raw_content_hash="sha256:" + "a" * 64,
        )
