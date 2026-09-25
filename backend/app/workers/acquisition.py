"""Durable external acquisition worker.

The HTTP layer only creates a queued acquisition.  This worker is the only
place that opens an external connection, and it hands successful bytes to the
existing ingestion queue with a deterministic job identity.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionJob, DownloadStatus
from app.knowledge.fetch_artifact import (
    ACQUISITION_CONTENT_TYPES,
    WEB_TEXT_PARSER_VERSION,
    AcquisitionArtifact,
)
from app.knowledge.fetch_policy import FetchPolicy, FetchPolicyError
from app.knowledge.fetcher import FetchResult, fetch_url
from app.knowledge.html_parser import HTML_PARSER_VERSION, html_to_markdown
from app.knowledge.models import (
    ACQUISITION_METHOD_WEB,
    MAX_DOCUMENT_BYTES,
    require_document_content,
)
from app.policy.taint import TaintSource

if TYPE_CHECKING:
    from app.platform import PlatformState

FetchCallable = Callable[[str], FetchResult]
OutcomeKind = Literal["idle", "succeeded", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: OutcomeKind
    acquisition_id: str = ""
    error_code: str = ""


class _FetchContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _artifact_from_fetch(job: AcquisitionJob, result: FetchResult) -> AcquisitionArtifact:
    content_type = result.content_type.split(";", 1)[0].strip().lower()
    if content_type not in ACQUISITION_CONTENT_TYPES:
        raise _FetchContractError(
            "FETCH_CONTENT_TYPE_UNSUPPORTED",
            "来源内容类型不受支持，未写入资料库",
        )
    if content_type != job.media_type and not (
        content_type == "text/html" and job.media_type == "text/markdown"
    ):
        raise _FetchContractError(
            "FETCH_CONTENT_TYPE_MISMATCH",
            "来源内容类型与用户选择不一致，未写入资料库",
        )
    if len(result.content) > MAX_DOCUMENT_BYTES:
        raise _FetchContractError("FETCH_PAYLOAD_TOO_LARGE", "来源响应超过大小上限")
    if not result.content:
        raise _FetchContractError("FETCH_CONTENT_INVALID", "来源正文为空，未写入资料库")
    parser_version = HTML_PARSER_VERSION if content_type == "text/html" else WEB_TEXT_PARSER_VERSION
    return AcquisitionArtifact(
        acquisition_id=job.acquisition_id,
        tenant_id=job.tenant_id,
        project_id=job.project_id,
        content_type=content_type,
        raw_content=result.content,
        parser_version=parser_version,
        fetched_at=datetime.now(timezone.utc),
    )


def _canonical_content(job: AcquisitionJob, artifact: AcquisitionArtifact) -> str:
    if artifact.content_type != job.media_type and not (
        artifact.content_type == "text/html" and job.media_type == "text/markdown"
    ):
        raise _FetchContractError(
            "FETCH_CONTENT_TYPE_MISMATCH",
            "来源内容类型与用户选择不一致，未写入资料库",
        )
    expected_parser = HTML_PARSER_VERSION if artifact.content_type == "text/html" else WEB_TEXT_PARSER_VERSION
    if artifact.parser_version != expected_parser:
        raise _FetchContractError(
            "FETCH_PARSER_VERSION_UNSUPPORTED",
            "已保存的来源解析器版本不受当前 worker 支持",
        )
    try:
        decoded = artifact.raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _FetchContractError(
            "FETCH_ENCODING_INVALID", "来源不是合法的 UTF-8 文本，未写入资料库"
        ) from exc
    try:
        if artifact.content_type == "text/html":
            content = html_to_markdown(decoded)
        else:
            content = require_document_content(decoded)
        return content
    except ValueError as exc:
        raise _FetchContractError("FETCH_CONTENT_INVALID", "来源正文不满足资料契约，未写入资料库") from exc


def _derived_ingestion_ids(acquisition_id: str) -> tuple[str, str]:
    return f"doc_web_{acquisition_id}", f"job_web_{acquisition_id}"


def _recovered_ingestion(
    platform: PlatformState,
    job: AcquisitionJob,
    *,
    document_id: str,
    ingestion_job_id: str,
    content: str,
    artifact: AcquisitionArtifact,
) -> None:
    """Accept a prior enqueue after a worker died before acquisition settle."""
    actor = Principal(principal_id=job.requested_by, tenant_id=job.tenant_id)
    try:
        stored_job = platform.ingestion.get_job(actor, job.project_id, ingestion_job_id)
        document = platform.ingestion.load_document(stored_job)
    except Exception as exc:  # noqa: BLE001 - recovery must surface one stable error
        raise PlatformError(
            ErrorCode.INTERNAL_CONSISTENCY_ERROR,
            "下载结果已部分写入但无法恢复摄取任务",
        ) from exc
    if (
        document.document_id != document_id
        or document.content != content
        or document.fetch_attempt_id != artifact.acquisition_id
        or document.source_content_type != artifact.content_type
        or document.raw_content_hash != artifact.content_hash
        or document.parser_version != artifact.parser_version
    ):
        raise PlatformError(
            ErrorCode.INTERNAL_CONSISTENCY_ERROR,
            "下载结果与既有摄取任务不一致",
        )


def run_once(
    platform: PlatformState,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    fetcher: FetchCallable | None = None,
    policy: FetchPolicy | None = None,
) -> Outcome:
    """Claim and process one acquisition; no retry is generated here."""
    job = platform.acquisition.claim_next(worker_id=worker_id, lease_seconds=lease_seconds)
    if job is None:
        return Outcome(kind="idle")

    effective_fetcher = fetcher or (
        lambda url: fetch_url(
            url,
            policy=policy or FetchPolicy(max_bytes=MAX_DOCUMENT_BYTES),
        )
    )
    try:
        artifact = platform.acquisition.load_artifact(job)
        if artifact is None:
            platform.acquisition.start_fetch_observation(job)
            fetch_started = time.monotonic()
            try:
                fetched = effective_fetcher(job.url)
            except FetchPolicyError as exc:
                duration_seconds = max(0.0, time.monotonic() - fetch_started)
                status = DownloadStatus.UNKNOWN if exc.retryable else DownloadStatus.FAILED
                platform.acquisition.finish_fetch_observation(
                    job,
                    outcome="unknown" if status is DownloadStatus.UNKNOWN else "failed",
                    duration_seconds=duration_seconds,
                    response_body_bytes=None,
                )
                platform.acquisition.settle(
                    job,
                    status=status,
                    error_code=exc.code,
                    safe_detail=exc.args[0] if exc.args else "来源请求未完成",
                    claim_token=job.claim_token,
                )
                return Outcome(
                    kind="unknown" if status is DownloadStatus.UNKNOWN else "failed",
                    acquisition_id=job.acquisition_id,
                    error_code=exc.code,
                )
            except Exception:
                platform.acquisition.finish_fetch_observation(
                    job,
                    outcome="unknown",
                    duration_seconds=max(0.0, time.monotonic() - fetch_started),
                    response_body_bytes=None,
                )
                raise
            else:
                platform.acquisition.finish_fetch_observation(
                    job,
                    outcome="succeeded",
                    duration_seconds=max(0.0, time.monotonic() - fetch_started),
                    response_body_bytes=len(fetched.content),
                )
            artifact = _artifact_from_fetch(job, fetched)
            artifact = platform.acquisition.save_artifact(job, artifact, claim_token=job.claim_token)
        content = _canonical_content(job, artifact)
        actor = Principal(principal_id=job.requested_by, tenant_id=job.tenant_id)
        document_id, ingestion_job_id = _derived_ingestion_ids(job.acquisition_id)
        try:
            platform.ingestion.enqueue(
                actor,
                job.project_id,
                job.source_id,
                document_id=document_id,
                job_id=ingestion_job_id,
                title=job.title,
                content=content,
                media_type=job.media_type,
                language=job.language,
                acquisition_method=ACQUISITION_METHOD_WEB,
                taint_sources=(TaintSource.WEB,),
                parser_version=artifact.parser_version,
                fetch_attempt_id=artifact.acquisition_id,
                source_content_type=artifact.content_type,
                raw_content_hash=artifact.content_hash,
            )
        except PlatformError as exc:
            if exc.code is not ErrorCode.VERSION_CONFLICT:
                raise
            _recovered_ingestion(
                platform,
                job,
                document_id=document_id,
                ingestion_job_id=ingestion_job_id,
                content=content,
                artifact=artifact,
            )
    except _FetchContractError as exc:
        platform.acquisition.settle(
            job,
            status=DownloadStatus.FAILED,
            error_code=exc.code,
            safe_detail=exc.detail,
            claim_token=job.claim_token,
        )
        return Outcome(kind="failed", acquisition_id=job.acquisition_id, error_code=exc.code)

    platform.acquisition.settle(
        job,
        status=DownloadStatus.SUCCEEDED,
        claim_token=job.claim_token,
    )
    return Outcome(kind="succeeded", acquisition_id=job.acquisition_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部资料下载 worker")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    parser.add_argument("--daemon", action="store_true", help="队列为空时继续轮询")
    parser.add_argument("--worker-id", default="", help="租约持有者标识")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--idle-sleep", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.lease_seconds <= 0 or args.idle_sleep < 0:
        parser.error("lease-seconds 必须为正数，idle-sleep 不能为负")
    if args.once and args.daemon:
        parser.error("--once 与 --daemon 不能同时使用")

    from app.core.ids import new_id
    from app.platform import build_platform

    platform = build_platform()
    worker_id = args.worker_id or new_id("acq-wk")
    while True:
        try:
            outcome = run_once(platform, worker_id=worker_id, lease_seconds=args.lease_seconds)
        except Exception as exc:  # noqa: BLE001 - supervisor must see non-zero exit
            print(
                f"[acquisition-worker {worker_id}] 任务未落定：{type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
        if outcome.kind != "idle":
            print(f"[acquisition-worker {worker_id}] {outcome.kind} {outcome.acquisition_id}")
        if args.once or outcome.kind == "idle" and not args.daemon:
            return 0
        time.sleep(args.idle_sleep)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
