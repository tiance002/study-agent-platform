"""Domain contracts for explicit source selection and bounded downloads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from app.core.contracts import require_aware, require_id, require_non_negative, require_text


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    SELECTED = "selected"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _require_source_url(url: str) -> str:
    require_text(url, "url")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("url 只允许 HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url 不得包含凭据")
    if parsed.hostname is None:
        raise ValueError("url 必须包含主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url 端口无效") from exc
    if port not in (None, 80, 443):
        raise ValueError("url 端口不在允许范围内")
    if any(char in url for char in "\r\n\t"):
        raise ValueError("url 包含禁止字符")
    return url


def _source_hostname(url: str) -> str:
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise ValueError("url 必须包含主机名")
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("url 主机名无效") from exc


def _require_status(value: object, expected: type[StrEnum], name: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{name} 必须是 {expected.__name__}")


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """A search result. It is not a source or a fact until explicitly selected."""

    candidate_id: str
    tenant_id: str
    project_id: str
    url: str
    title: str
    snippet: str
    source_domain: str
    discovered_at: datetime
    expires_at: datetime
    status: CandidateStatus = CandidateStatus.DISCOVERED

    def __post_init__(self) -> None:
        require_id(self.candidate_id, "candidate_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        _require_source_url(self.url)
        require_text(self.title, "title")
        require_text(self.snippet, "snippet", allow_empty=True)
        require_text(self.source_domain, "source_domain")
        if self.source_domain.rstrip(".").lower() != _source_hostname(self.url):
            raise ValueError("source_domain 必须与 url 主机名一致")
        require_aware(self.discovered_at, "discovered_at")
        require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.discovered_at:
            raise ValueError("expires_at 必须晚于 discovered_at")
        _require_status(self.status, CandidateStatus, "status")


@dataclass(frozen=True, slots=True)
class AcquisitionJob:
    """A durable download claim, separate from the later parsing job."""

    acquisition_id: str
    tenant_id: str
    project_id: str
    source_id: str
    candidate_id: str
    url: str
    title: str
    media_type: str
    language: str
    status: DownloadStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    lease_owner: str = ""
    lease_until: datetime | None = None
    claim_token: str = ""
    error_code: str = ""
    error_detail: str = ""

    def __post_init__(self) -> None:
        for name in (
            "acquisition_id",
            "tenant_id",
            "project_id",
            "source_id",
            "candidate_id",
        ):
            require_id(getattr(self, name), name)
        _require_source_url(self.url)
        require_text(self.title, "title")
        if self.media_type not in {"text/plain", "text/markdown", "text/html"}:
            raise ValueError("media_type 不在首版允许范围内")
        require_text(self.language, "language")
        _require_status(self.status, DownloadStatus, "status")
        require_non_negative(self.attempt_count, "attempt_count")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        require_text(self.lease_owner, "lease_owner", allow_empty=True)
        require_text(self.claim_token, "claim_token", allow_empty=True)
        require_text(self.error_code, "error_code", allow_empty=True)
        require_text(self.error_detail, "error_detail", allow_empty=True)
        has_lease = bool(self.lease_owner and self.claim_token and self.lease_until)
        if (self.status is DownloadStatus.RUNNING) != has_lease:
            raise ValueError("running acquisition job 必须有完整 lease")
        if self.status is not DownloadStatus.RUNNING and (
            self.lease_owner or self.claim_token or self.lease_until is not None
        ):
            raise ValueError("非 running acquisition job 不得携带 lease")
        if self.status in {DownloadStatus.FAILED, DownloadStatus.UNKNOWN} and not self.error_code:
            raise ValueError("失败或未知 acquisition job 必须有 error_code")


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    """The explicit selection command that creates one download job."""

    acquisition_id: str
    tenant_id: str
    project_id: str
    source_id: str
    candidate_id: str
    requested_by: str
    url: str
    title: str
    media_type: str
    language: str
    idempotency_key: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "acquisition_id",
            "tenant_id",
            "project_id",
            "source_id",
            "candidate_id",
            "requested_by",
            "idempotency_key",
        ):
            require_id(getattr(self, name), name)
        _require_source_url(self.url)
        require_text(self.title, "title")
        if self.media_type not in {"text/plain", "text/markdown", "text/html"}:
            raise ValueError("media_type 不在首版允许范围内")
        require_text(self.language, "language")
        require_aware(self.requested_at, "requested_at")

    def to_queued_job(self) -> AcquisitionJob:
        return AcquisitionJob(
            acquisition_id=self.acquisition_id,
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            source_id=self.source_id,
            candidate_id=self.candidate_id,
            url=self.url,
            title=self.title,
            media_type=self.media_type,
            language=self.language,
            status=DownloadStatus.QUEUED,
            attempt_count=0,
            created_at=self.requested_at,
            updated_at=self.requested_at,
        )


_CANDIDATE_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.DISCOVERED: frozenset(
        {CandidateStatus.SELECTED, CandidateStatus.REJECTED, CandidateStatus.EXPIRED}
    ),
    CandidateStatus.SELECTED: frozenset({CandidateStatus.REJECTED, CandidateStatus.EXPIRED}),
    CandidateStatus.REJECTED: frozenset(),
    CandidateStatus.EXPIRED: frozenset(),
}

_DOWNLOAD_TRANSITIONS: dict[DownloadStatus, frozenset[DownloadStatus]] = {
    DownloadStatus.QUEUED: frozenset({DownloadStatus.RUNNING}),
    DownloadStatus.RUNNING: frozenset(
        {DownloadStatus.SUCCEEDED, DownloadStatus.FAILED, DownloadStatus.UNKNOWN}
    ),
    DownloadStatus.SUCCEEDED: frozenset(),
    DownloadStatus.FAILED: frozenset(),
    DownloadStatus.UNKNOWN: frozenset(),
}


def claim_download(
    job: AcquisitionJob,
    *,
    lease_owner: str,
    lease_until: datetime,
    claim_token: str,
) -> AcquisitionJob:
    """Create a running claim; durable repositories must make this atomic."""

    if job.status is not DownloadStatus.QUEUED:
        raise ValueError(f"download claim requires queued status, got {job.status}")
    require_text(lease_owner, "lease_owner")
    require_text(claim_token, "claim_token")
    require_aware(lease_until, "lease_until")
    if lease_until <= job.updated_at:
        raise ValueError("lease_until 必须晚于 updated_at")
    return replace(
        job,
        status=DownloadStatus.RUNNING,
        attempt_count=job.attempt_count + 1,
        lease_owner=lease_owner,
        lease_until=lease_until,
        claim_token=claim_token,
    )


def transition_candidate(candidate: SourceCandidate, next_status: CandidateStatus) -> SourceCandidate:
    _require_status(next_status, CandidateStatus, "next_status")
    if next_status not in _CANDIDATE_TRANSITIONS[candidate.status]:
        raise ValueError(f"candidate transition {candidate.status} -> {next_status} is not allowed")
    return replace(candidate, status=next_status)


def transition_download(job: AcquisitionJob, next_status: DownloadStatus) -> AcquisitionJob:
    _require_status(next_status, DownloadStatus, "next_status")
    if next_status not in _DOWNLOAD_TRANSITIONS[job.status]:
        raise ValueError(f"download transition {job.status} -> {next_status} is not allowed")
    if next_status is DownloadStatus.RUNNING:
        raise ValueError("running transition must be created by the worker claim operation")
    if next_status in {DownloadStatus.FAILED, DownloadStatus.UNKNOWN}:
        return replace(
            job,
            status=next_status,
            lease_owner="",
            lease_until=None,
            claim_token="",
            error_code="ACQUISITION_FAILED",
        )
    return replace(job, status=next_status, lease_owner="", lease_until=None, claim_token="")
