"""In-memory acquisition adapter used by development and contract tests."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import timedelta

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.knowledge.acquisition import (
    ACQUISITION_ATTEMPTS_EXHAUSTED,
    ACQUISITION_ATTEMPTS_EXHAUSTED_DETAIL,
    MAX_ACQUISITION_ATTEMPTS,
    AcquisitionJob,
    AcquisitionRequest,
    CandidateStatus,
    DownloadStatus,
    SourceCandidate,
    claim_download,
    transition_download,
)
from app.knowledge.acquisition_ports import AcquisitionRepository
from app.knowledge.fetch_artifact import AcquisitionArtifact
from app.product.ports import ProductRepository


class InMemoryAcquisitionRepository(AcquisitionRepository):
    """Candidate and download state with the same fences as the PG adapter."""

    def __init__(
        self,
        *,
        membership: MembershipRepository,
        products: ProductRepository,
        clock: Clock | None = None,
    ) -> None:
        self.membership = membership
        self.products = products
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._candidates: dict[str, SourceCandidate] = {}
        self._jobs: dict[str, AcquisitionJob] = {}
        self._artifacts: dict[str, AcquisitionArtifact] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

    def save_candidate(self, candidate: SourceCandidate) -> SourceCandidate:
        with self._lock:
            existing = self._candidates.get(candidate.candidate_id)
            if existing is not None and existing != candidate:
                raise deny(ErrorCode.PARAMS_INVALID, "候选标识已存在且内容不一致")
            self._candidates[candidate.candidate_id] = candidate
        return candidate

    def create_candidate(
        self, actor: Principal, project_id: str, candidate: SourceCandidate
    ) -> SourceCandidate:
        self.membership.get(actor, project_id)
        if (
            candidate.tenant_id != actor.tenant_id
            or candidate.project_id != project_id
            or candidate.status is not CandidateStatus.DISCOVERED
        ):
            raise deny(ErrorCode.CROSS_PROJECT_DENIED, "无权创建该项目候选资料")
        return self.save_candidate(candidate)

    def list_candidates(self, actor: Principal, project_id: str) -> tuple[SourceCandidate, ...]:
        self.membership.get(actor, project_id)
        with self._lock:
            rows = [
                row
                for row in self._candidates.values()
                if row.tenant_id == actor.tenant_id and row.project_id == project_id
            ]
        return tuple(sorted(rows, key=lambda row: (row.discovered_at, row.candidate_id), reverse=True))

    def get_candidate(self, actor: Principal, project_id: str, candidate_id: str) -> SourceCandidate:
        self.membership.get(actor, project_id)
        with self._lock:
            candidate = self._candidates.get(candidate_id)
        if candidate is None or candidate.tenant_id != actor.tenant_id or candidate.project_id != project_id:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", candidate_id=candidate_id)
        return candidate

    def select(
        self,
        actor: Principal,
        project_id: str,
        request: AcquisitionRequest,
    ) -> AcquisitionJob:
        self.membership.get(actor, project_id)
        self.products.get_source(actor, project_id, request.source_id)
        if (
            request.tenant_id != actor.tenant_id
            or request.project_id != project_id
            or request.requested_by != actor.principal_id
        ):
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权创建该下载任务")
        key = (actor.tenant_id, project_id, request.idempotency_key)
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if (
                    existing.candidate_id != request.candidate_id
                    or existing.source_id != request.source_id
                    or existing.url != request.url
                ):
                    raise deny(ErrorCode.PARAMS_INVALID, "同一幂等键对应了不同下载请求")
                return existing
            candidate = self._candidates.get(request.candidate_id)
            if (
                candidate is None
                or candidate.tenant_id != actor.tenant_id
                or candidate.project_id != project_id
            ):
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目")
            if candidate.status is not CandidateStatus.DISCOVERED:
                raise deny(ErrorCode.ILLEGAL_STATE_TRANSITION, "候选资料已经处理")
            if candidate.url != request.url or candidate.title != request.title:
                raise deny(ErrorCode.PARAMS_INVALID, "下载请求与候选资料不一致")
            job = request.to_queued_job()
            self._candidates[candidate.candidate_id] = replace(candidate, status=CandidateStatus.SELECTED)
            self._jobs[job.acquisition_id] = job
            self._idempotency[key] = job.acquisition_id
            return job

    def get_job(self, actor: Principal, project_id: str, acquisition_id: str) -> AcquisitionJob:
        self.membership.get(actor, project_id)
        with self._lock:
            job = self._jobs.get(acquisition_id)
        if job is None or job.tenant_id != actor.tenant_id or job.project_id != project_id:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", acquisition_id=acquisition_id)
        return job

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> AcquisitionJob | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正整数")
        now = self.clock.now()
        with self._lock:
            for acquisition_id, row in tuple(self._jobs.items()):
                if (
                    row.status is DownloadStatus.RUNNING
                    and row.lease_until is not None
                    and now >= row.lease_until
                    and row.attempt_count >= MAX_ACQUISITION_ATTEMPTS
                ):
                    settled = transition_download(row, DownloadStatus.UNKNOWN)
                    self._jobs[acquisition_id] = replace(
                        settled,
                        error_code=ACQUISITION_ATTEMPTS_EXHAUSTED,
                        error_detail=ACQUISITION_ATTEMPTS_EXHAUSTED_DETAIL,
                        updated_at=now,
                    )
            candidates = [
                row
                for row in self._jobs.values()
                if row.status is DownloadStatus.QUEUED
                and row.attempt_count < MAX_ACQUISITION_ATTEMPTS
                or (
                    row.status is DownloadStatus.RUNNING
                    and row.lease_until is not None
                    and now >= row.lease_until
                    and row.attempt_count < MAX_ACQUISITION_ATTEMPTS
                )
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda row: (row.created_at, row.acquisition_id))
            current = candidates[0]
            if current.status is DownloadStatus.RUNNING:
                current = replace(
                    current,
                    status=DownloadStatus.QUEUED,
                    lease_owner="",
                    lease_until=None,
                    claim_token="",
                    updated_at=now,
                )
            claimed = claim_download(
                current,
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
                claim_token=uuid.uuid4().hex,
            )
            claimed = replace(claimed, updated_at=now)
            self._jobs[claimed.acquisition_id] = claimed
            return claimed

    def load_artifact(self, job: AcquisitionJob) -> AcquisitionArtifact | None:
        with self._lock:
            artifact = self._artifacts.get(job.acquisition_id)
        if artifact is None:
            return None
        if artifact.tenant_id != job.tenant_id or artifact.project_id != job.project_id:
            raise deny(ErrorCode.CROSS_PROJECT_DENIED, "下载原文作用域与任务不一致")
        return artifact

    def save_artifact(
        self,
        job: AcquisitionJob,
        artifact: AcquisitionArtifact,
        *,
        claim_token: str,
    ) -> AcquisitionArtifact:
        if (
            artifact.acquisition_id != job.acquisition_id
            or artifact.tenant_id != job.tenant_id
            or artifact.project_id != job.project_id
        ):
            raise deny(ErrorCode.CROSS_PROJECT_DENIED, "下载原文作用域与任务不一致")
        with self._lock:
            current = self._jobs.get(job.acquisition_id)
            if (
                current is None
                or current.tenant_id != job.tenant_id
                or current.project_id != job.project_id
                or current.status is not DownloadStatus.RUNNING
                or current.claim_token != claim_token
                or current.lease_until is None
                or self.clock.now() >= current.lease_until
            ):
                raise deny(ErrorCode.ILLEGAL_STATE_TRANSITION, "下载任务认领已经失效")
            existing = self._artifacts.get(job.acquisition_id)
            if existing is not None and existing != artifact:
                raise deny(ErrorCode.PARAMS_INVALID, "同一下载任务的原始响应不可覆盖")
            self._artifacts[job.acquisition_id] = artifact
            return existing or artifact

    def settle(
        self,
        job: AcquisitionJob,
        *,
        status: object,
        error_code: str = "",
        safe_detail: str = "",
        claim_token: str,
    ) -> None:
        if not isinstance(status, DownloadStatus) or status not in {
            DownloadStatus.SUCCEEDED,
            DownloadStatus.FAILED,
            DownloadStatus.UNKNOWN,
        }:
            raise ValueError("settle status 必须是 succeeded、failed 或 unknown")
        with self._lock:
            current = self._jobs.get(job.acquisition_id)
            if current is None:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "下载任务不存在")
            if current.status is status:
                return
            if (
                current.status is not DownloadStatus.RUNNING
                or current.claim_token != claim_token
                or current.lease_until is None
                or self.clock.now() >= current.lease_until
            ):
                raise deny(ErrorCode.ILLEGAL_STATE_TRANSITION, "下载任务认领已经失效")
            if status is DownloadStatus.SUCCEEDED:
                settled = transition_download(current, status)
            else:
                if not error_code or not safe_detail:
                    raise ValueError("失败或未知下载必须带安全错误信息")
                settled = replace(
                    transition_download(current, status),
                    error_code=error_code,
                    error_detail=safe_detail,
                )
            self._jobs[settled.acquisition_id] = replace(
                settled,
                updated_at=self.clock.now(),
            )
