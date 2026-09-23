"""Ports for candidate selection and durable source acquisition."""

from __future__ import annotations

from typing import Protocol

from app.identity.models import Principal
from app.knowledge.acquisition import (
    AcquisitionJob,
    AcquisitionRequest,
    DownloadStatus,
    SourceCandidate,
)
from app.knowledge.fetch_artifact import AcquisitionArtifact


class AcquisitionRepository(Protocol):
    def create_candidate(
        self, actor: Principal, project_id: str, candidate: SourceCandidate
    ) -> SourceCandidate: ...

    def list_candidates(self, actor: Principal, project_id: str) -> tuple[SourceCandidate, ...]: ...

    def save_candidate(self, candidate: SourceCandidate) -> SourceCandidate: ...

    def get_candidate(self, actor: Principal, project_id: str, candidate_id: str) -> SourceCandidate: ...

    def select(
        self,
        actor: Principal,
        project_id: str,
        request: AcquisitionRequest,
    ) -> AcquisitionJob: ...

    def get_job(self, actor: Principal, project_id: str, acquisition_id: str) -> AcquisitionJob: ...

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> AcquisitionJob | None: ...

    def load_artifact(self, job: AcquisitionJob) -> AcquisitionArtifact | None: ...

    def save_artifact(
        self, job: AcquisitionJob, artifact: AcquisitionArtifact, *, claim_token: str
    ) -> AcquisitionArtifact: ...

    def settle(
        self,
        job: AcquisitionJob,
        *,
        status: DownloadStatus,
        error_code: str = "",
        safe_detail: str = "",
        claim_token: str,
    ) -> None: ...
