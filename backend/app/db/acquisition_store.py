"""PostgreSQL adapter for explicit source selection and acquisition jobs."""

from __future__ import annotations

from psycopg import errors as pg_errors

from app.core.clock import Clock, SystemClock
from app.core.contracts import require_text
from app.core.errors import ErrorCode, PlatformError, deny
from app.db.session import tenant_transaction, worker_transaction
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.knowledge.acquisition import (
    AcquisitionJob,
    AcquisitionRequest,
    CandidateStatus,
    DownloadStatus,
    SourceCandidate,
)
from app.knowledge.acquisition_ports import AcquisitionRepository
from app.product.ports import ProductRepository

_CANDIDATE_COLUMNS = (
    "candidate_id, tenant_id, project_id, url, title, snippet, source_domain,"
    " status, discovered_at, expires_at"
)
_JOB_COLUMNS = (
    "acquisition_id, tenant_id, project_id, source_id, candidate_id, requested_by,"
    " url, title, media_type, language, idempotency_key, status, attempt_count,"
    " lease_owner, lease_until, claim_token, error_code, error_detail, created_at, updated_at"
)
_JOB_INSERT_COLUMNS = (
    "acquisition_id, tenant_id, project_id, source_id, candidate_id, requested_by,"
    " url, title, media_type, language, idempotency_key, status, attempt_count"
)
_CLAIM_SELECT = """
SELECT acquisition_id FROM acquisition_jobs
 WHERE status = 'queued'
    OR (status = 'running' AND lease_until < now())
 ORDER BY created_at, acquisition_id
 FOR UPDATE SKIP LOCKED
 LIMIT 1
"""


def _candidate_from_row(row: tuple) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        url=row[3],
        title=row[4],
        snippet=row[5],
        source_domain=row[6],
        status=CandidateStatus(row[7]),
        discovered_at=row[8],
        expires_at=row[9],
    )


def _job_from_row(row: tuple) -> AcquisitionJob:
    return AcquisitionJob(
        acquisition_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        source_id=row[3],
        candidate_id=row[4],
        requested_by=row[5],
        url=row[6],
        title=row[7],
        media_type=row[8],
        language=row[9],
        idempotency_key=row[10],
        status=DownloadStatus(row[11]),
        attempt_count=row[12],
        lease_owner=row[13] or "",
        lease_until=row[14],
        claim_token=str(row[15]) if row[15] is not None else "",
        error_code=row[16],
        error_detail=row[17],
        created_at=row[18],
        updated_at=row[19],
    )


class PostgresAcquisitionRepository(AcquisitionRepository):
    def __init__(
        self,
        *,
        membership: MembershipRepository | None = None,
        products: ProductRepository | None = None,
        clock: Clock | None = None,
        dsn: str | None = None,
        worker_dsn: str | None = None,
    ) -> None:
        self.membership = membership
        self.products = products
        self.clock = clock or SystemClock()
        self._dsn = dsn
        self._worker_dsn = worker_dsn

    def save_candidate(self, candidate: SourceCandidate) -> SourceCandidate:
        with worker_transaction(
            tenant_id=candidate.tenant_id,
            project_id=candidate.project_id,
            dsn=self._worker_dsn,
        ) as conn:
            conn.execute(
                "INSERT INTO source_candidates ("
                + _CANDIDATE_COLUMNS
                + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (candidate_id) DO NOTHING",
                (
                    candidate.candidate_id,
                    candidate.tenant_id,
                    candidate.project_id,
                    candidate.url,
                    candidate.title,
                    candidate.snippet,
                    candidate.source_domain,
                    str(candidate.status),
                    candidate.discovered_at,
                    candidate.expires_at,
                ),
            )
            row = conn.execute(
                "SELECT " + _CANDIDATE_COLUMNS + " FROM source_candidates WHERE candidate_id = %s",
                (candidate.candidate_id,),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "候选资料写入后无法回读")
        stored = _candidate_from_row(row)
        if stored != candidate:
            raise deny(ErrorCode.PARAMS_INVALID, "候选标识已存在且内容不一致")
        return stored

    def create_candidate(
        self, actor: Principal, project_id: str, candidate: SourceCandidate
    ) -> SourceCandidate:
        self._require_membership(actor, project_id)
        if (
            candidate.tenant_id != actor.tenant_id
            or candidate.project_id != project_id
            or candidate.status is not CandidateStatus.DISCOVERED
        ):
            raise deny(ErrorCode.CROSS_PROJECT_DENIED, "无权创建该项目候选资料")
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            try:
                conn.execute(
                    "INSERT INTO source_candidates (" + _CANDIDATE_COLUMNS + ")"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        candidate.candidate_id,
                        candidate.tenant_id,
                        candidate.project_id,
                        candidate.url,
                        candidate.title,
                        candidate.snippet,
                        candidate.source_domain,
                        str(candidate.status),
                        candidate.discovered_at,
                        candidate.expires_at,
                    ),
                )
            except pg_errors.UniqueViolation as exc:
                raise deny(ErrorCode.PARAMS_INVALID, "候选标识已存在") from exc
        return candidate

    def list_candidates(
        self, actor: Principal, project_id: str
    ) -> tuple[SourceCandidate, ...]:
        self._require_membership(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _CANDIDATE_COLUMNS
                + " FROM source_candidates ORDER BY discovered_at DESC, candidate_id DESC"
            ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def get_candidate(
        self, actor: Principal, project_id: str, candidate_id: str
    ) -> SourceCandidate:
        self._require_membership(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _CANDIDATE_COLUMNS + " FROM source_candidates WHERE candidate_id = %s",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", candidate_id=candidate_id)
        return _candidate_from_row(row)

    def select(
        self,
        actor: Principal,
        project_id: str,
        request: AcquisitionRequest,
    ) -> AcquisitionJob:
        self._require_membership(actor, project_id)
        self._require_products().get_source(actor, project_id, request.source_id)
        if (
            request.tenant_id != actor.tenant_id
            or request.project_id != project_id
            or request.requested_by != actor.principal_id
        ):
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权创建该下载任务")

        try:
            with tenant_transaction(
                tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
            ) as conn:
                existing = conn.execute(
                    "SELECT " + _JOB_COLUMNS
                    + " FROM acquisition_jobs WHERE tenant_id = %s"
                    " AND project_id = %s AND idempotency_key = %s",
                    (actor.tenant_id, project_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._assert_idempotent_match(_job_from_row(existing), request)

                candidate_row = conn.execute(
                    "SELECT " + _CANDIDATE_COLUMNS
                    + " FROM source_candidates WHERE candidate_id = %s FOR UPDATE",
                    (request.candidate_id,),
                ).fetchone()
                if candidate_row is None:
                    raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目")
                candidate = _candidate_from_row(candidate_row)
                if candidate.status is not CandidateStatus.DISCOVERED:
                    raise deny(ErrorCode.ILLEGAL_STATE_TRANSITION, "候选资料已经处理")
                if candidate.url != request.url or candidate.title != request.title:
                    raise deny(ErrorCode.PARAMS_INVALID, "下载请求与候选资料不一致")

                # The candidate row lock serializes first-use selection.  Re-read the
                # idempotency key after waiting so a concurrent identical request
                # returns the committed job instead of seeing only the selected state.
                existing = conn.execute(
                    "SELECT " + _JOB_COLUMNS
                    + " FROM acquisition_jobs WHERE tenant_id = %s"
                    " AND project_id = %s AND idempotency_key = %s",
                    (actor.tenant_id, project_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._assert_idempotent_match(_job_from_row(existing), request)

                row = conn.execute(
                    "INSERT INTO acquisition_jobs ("
                    + _JOB_INSERT_COLUMNS
                    + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " RETURNING "
                    + _JOB_COLUMNS,
                    (
                        request.acquisition_id,
                        request.tenant_id,
                        request.project_id,
                        request.source_id,
                        request.candidate_id,
                        request.requested_by,
                        request.url,
                        request.title,
                        request.media_type,
                        request.language,
                        request.idempotency_key,
                        str(DownloadStatus.QUEUED),
                        0,
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE source_candidates SET status = %s WHERE candidate_id = %s",
                    (str(CandidateStatus.SELECTED), request.candidate_id),
                )
                if row is None:
                    raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "下载任务写入后无法回读")
                return _job_from_row(row)
        except pg_errors.UniqueViolation as exc:
            raise PlatformError(
                ErrorCode.VERSION_CONFLICT,
                "下载任务被并发创建；请使用原幂等键重试",
                retryable=True,
            ) from exc

    def get_job(
        self, actor: Principal, project_id: str, acquisition_id: str
    ) -> AcquisitionJob:
        self._require_membership(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _JOB_COLUMNS + " FROM acquisition_jobs WHERE acquisition_id = %s",
                (acquisition_id,),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", acquisition_id=acquisition_id)
        return _job_from_row(row)

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> AcquisitionJob | None:
        require_text(worker_id, "worker_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正整数")
        with worker_transaction(dsn=self._worker_dsn) as conn:
            conn.execute("SELECT set_config('app.worker_id', %s, true)", (worker_id,))
            selected = conn.execute(_CLAIM_SELECT).fetchone()
            if selected is None:
                return None
            row = conn.execute(
                "UPDATE acquisition_jobs"
                " SET status = 'running', attempt_count = attempt_count + 1,"
                "     lease_owner = %s, lease_until = now() + make_interval(secs => %s),"
                "     claim_token = gen_random_uuid(), updated_at = now()"
                " WHERE acquisition_id = %s"
                " RETURNING "
                + _JOB_COLUMNS,
                (worker_id, float(lease_seconds), selected[0]),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "下载任务认领后无法回读")
        return _job_from_row(row)

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
        if status in {DownloadStatus.FAILED, DownloadStatus.UNKNOWN} and (
            not error_code or not safe_detail
        ):
            raise ValueError("失败或未知下载必须带安全错误信息")
        with worker_transaction(
            tenant_id=job.tenant_id,
            project_id=job.project_id,
            dsn=self._worker_dsn,
        ) as conn:
            result = conn.execute(
                "UPDATE acquisition_jobs"
                " SET status = %s, lease_owner = NULL, lease_until = NULL,"
                "     claim_token = NULL, error_code = %s, error_detail = %s, updated_at = now()"
                " WHERE acquisition_id = %s AND status = 'running'"
                "   AND claim_token = %s AND lease_until > now()"
                " RETURNING acquisition_id",
                (
                    str(status),
                    error_code if status is not DownloadStatus.SUCCEEDED else "",
                    safe_detail if status is not DownloadStatus.SUCCEEDED else "",
                    job.acquisition_id,
                    claim_token,
                ),
            ).fetchone()
            if result is not None:
                return
            current = conn.execute(
                "SELECT status FROM acquisition_jobs WHERE acquisition_id = %s FOR UPDATE",
                (job.acquisition_id,),
            ).fetchone()
            if current is not None and current[0] == str(status):
                return
            raise deny(ErrorCode.ILLEGAL_STATE_TRANSITION, "下载任务认领已经失效")

    def _require_membership(self, actor: Principal, project_id: str) -> None:
        if self.membership is None:
            raise RuntimeError("PostgresAcquisitionRepository requires membership")
        self.membership.get(actor, project_id)

    def _require_products(self) -> ProductRepository:
        if self.products is None:
            raise RuntimeError("PostgresAcquisitionRepository requires products")
        return self.products

    def _assert_idempotent_match(
        self, existing: AcquisitionJob, request: AcquisitionRequest
    ) -> AcquisitionJob:
        if (
            existing.candidate_id != request.candidate_id
            or existing.source_id != request.source_id
            or existing.url != request.url
        ):
            raise deny(ErrorCode.PARAMS_INVALID, "同一幂等键对应了不同下载请求")
        return existing
