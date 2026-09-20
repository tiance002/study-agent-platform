"""摄取仓储的内存实现（开发适配器）。

⚠️ 与 `product/memory_store.py` 一样，这是**开发适配器**：没有 RLS，
隔离靠本类的判定；PostgreSQL 实现由数据库兜底。两者跑同一套契约测试。

## 三条不能因为"内存版简单"就省掉的语义

1. **认领必须原子**：`claim_next` 的"选 + 占"在同一个锁内完成。
   分开写（先挑再改）就是 check-then-act —— 两个 worker 会认领同一个任务，
   而同一条路径在 PostgreSQL 里靠 `FOR UPDATE SKIP LOCKED` 才成立。
2. **`complete` 是幂等成功**：任务已是 `succeeded` 时直接返回，不追加片段。
   重复投递是真实场景（写完被杀、消息重投），不是异常。
3. **片段作用域必须与任务一致**：片段自带租户/项目/文档，全部必须等于任务的那三个。
   少了这道判定，"worker 把 A 项目的片段挂到 B 项目的原文上"就只是一个
   参数写错，而它会静默产生一条**引用指向别人资料**的片段。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import timedelta

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.knowledge.models import (
    ACQUISITION_METHOD_UPLOAD,
    DOCUMENT_PARSER_VERSION,
    IngestionJob,
    IngestionStatus,
    SourceDocument,
    StoredChunk,
    assert_chunks_belong_to_job,
    assert_chunks_match_document,
)
from app.product.ports import ProductRepository


class InMemoryIngestionRepository:
    """原文 / 任务 / 片段的内存存储。

    三个字典共用一把 `RLock`：它们的原子性要求是互相牵连的
    （认领要改任务同时读片段冲突、完成要写片段同时改任务），
    各自的锁只会让人以为"已经安全了"。
    """

    def __init__(
        self,
        *,
        membership: MembershipRepository,
        products: ProductRepository,
        clock: Clock | None = None,
    ) -> None:
        self.membership = membership
        self.products = products
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._documents: dict[str, SourceDocument] = {}
        self._jobs: dict[str, IngestionJob] = {}
        self._chunks: dict[str, StoredChunk] = {}
        #: 文档内片段序号占用表，镜像 `UNIQUE (document_id, chunk_index)`。
        self._chunk_slots: dict[tuple[str, int], str] = {}

    # ------------------------------------------------------------------ 写入

    def enqueue(
        self,
        actor: Principal,
        project_id: str,
        source_id: str,
        *,
        document_id: str,
        job_id: str,
        title: str,
        content: str,
        media_type: str,
        language: str,
    ) -> tuple[SourceDocument, IngestionJob]:
        # 资料必须存在且属于该项目 —— 内存版没有组合外键兜底，
        # 这一句就是"不能给别的项目的资料挂原文"的唯一防线。
        self.products.get_source(actor, project_id, source_id)
        now = self._clock.now()
        document = SourceDocument(
            document_id=document_id,
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source_id,
            version=1,  # 占位：真实版本在锁内分配
            document_title=title,
            content=content,
            media_type=media_type,
            language=language,
            observed_at=now,
            parser_version=DOCUMENT_PARSER_VERSION,
            acquisition_method=ACQUISITION_METHOD_UPLOAD,
        )
        with self._lock:
            version = (
                max(
                    (
                        row.version
                        for row in self._documents.values()
                        if row.source_id == source_id
                    ),
                    default=0,
                )
                + 1
            )
            document = replace(document, version=version)
            job = IngestionJob(
                job_id=job_id,
                tenant_id=actor.tenant_id,
                project_id=project_id,
                source_id=source_id,
                document_id=document_id,
                status=IngestionStatus.QUEUED,
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
            # 两行一起进内存：中途不会失败，但顺序刻意与 PostgreSQL 一致
            # （原文先、任务后，任务的外键指向原文）。
            self._documents[document_id] = document
            self._jobs[job_id] = job
        return document, job

    # ------------------------------------------------------------------ 读取

    def get_job(self, actor: Principal, project_id: str, job_id: str) -> IngestionJob:
        self.membership.get(actor, project_id)
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.project_id != project_id or job.tenant_id != actor.tenant_id:
            # 不可见与不存在同码同话术：区分原因等于提供存在性探针。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", job_id=job_id
            )
        return job

    def load_document(self, job: IngestionJob) -> SourceDocument:
        with self._lock:
            document = self._documents.get(job.document_id)
        if (
            document is None
            or document.tenant_id != job.tenant_id
            or document.project_id != job.project_id
        ):
            raise deny(
                ErrorCode.CROSS_PROJECT_DENIED,
                "任务对应的原文不在该任务的作用域内",
                document_id=job.document_id,
            )
        return document

    def stored_chunks(
        self, actor: Principal, project_id: str, *, latest_only: bool = True
    ) -> tuple[StoredChunk, ...]:
        """该项目下的片段。`latest_only` 时每个来源只取最新版本。

        **过滤在方法内完成** —— 调用方不需要、也不应该自己判断作用域
        （把隔离交给"记得写"迟早会漏）。
        """
        self.membership.get(actor, project_id)
        with self._lock:
            rows = [
                chunk
                for chunk in self._chunks.values()
                if chunk.tenant_id == actor.tenant_id and chunk.project_id == project_id
            ]
            if latest_only:
                rows = self._only_latest_versions(rows)
        rows.sort(key=lambda chunk: (chunk.document_id, chunk.chunk_index))
        return tuple(rows)

    def chunk_at(
        self,
        actor: Principal,
        project_id: str,
        *,
        document_id: str,
        span: tuple[int, int],
    ) -> StoredChunk | None:
        """按不可变标识精确回读。**不挑"第一条匹配"**（见端口契约）。"""
        self.membership.get(actor, project_id)
        with self._lock:
            for chunk in self._chunks.values():
                if (
                    chunk.tenant_id == actor.tenant_id
                    and chunk.project_id == project_id
                    and chunk.document_id == document_id
                    and chunk.span == span
                ):
                    return chunk
        return None

    def _only_latest_versions(
        self, chunks: list[StoredChunk]
    ) -> list[StoredChunk]:
        """每个来源只保留**版本号最大**的那一版片段。

        与 PostgreSQL 版的 `DISTINCT ON (source_id) ... ORDER BY version DESC`
        同一语义：版本号来自 `_documents`（片段本身不存版本号）。
        只保留"有片段的版本"—— 一个失败的版本没有片段，不会把旧版本挤掉。
        """
        newest: dict[str, tuple[int, str]] = {}
        for chunk in chunks:
            document = self._documents.get(chunk.document_id)
            if document is None:  # pragma: no cover - 片段必有原文，缺了是内部不一致
                continue
            current = newest.get(chunk.source_id)
            if current is None or document.version > current[0]:
                newest[chunk.source_id] = (document.version, chunk.document_id)
        keep = {document_id for _version, document_id in newest.values()}
        return [chunk for chunk in chunks if chunk.document_id in keep]

    # ------------------------------------------------------------------ 队列

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> IngestionJob | None:
        """认领下一个可做的任务。**整个"选 + 占"在一把锁内完成。**"""
        with self._lock:
            now = self._clock.now()
            candidates = [
                job
                for job in self._jobs.values()
                if job.status is IngestionStatus.QUEUED
                or (
                    job.status is IngestionStatus.PROCESSING
                    and job.lease_until is not None
                    and now >= job.lease_until
                )
            ]
            if not candidates:
                return None
            # 与 SQL 的 ORDER BY created_at, job_id 同一顺序：
            # 先来先做，同刻按 id 定序 —— 否则"哪条先被处理"不可复现。
            candidates.sort(key=lambda job: (job.created_at, job.job_id))
            job = candidates[0]
            claimed = replace(
                job,
                status=IngestionStatus.PROCESSING,
                attempt_count=job.attempt_count + 1,
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
                # 每次认领一个新的、不可复用的围栏 token（见 `complete`）。
                claim_token=uuid.uuid4().hex,
                updated_at=now,
            )
            self._jobs[job.job_id] = claimed
            return claimed

    def complete(self, job: IngestionJob, chunks: tuple[StoredChunk, ...]) -> None:
        assert_chunks_belong_to_job(job, chunks)
        with self._lock:
            current = self._jobs.get(job.job_id)
            if current is None:
                raise deny(
                    ErrorCode.CROSS_TENANT_DENIED, "任务不存在", job_id=job.job_id
                )
            if not self._holds_settlement_rights(current, job):
                self._reject_if_claim_is_stale(current, job, action="complete")
                # 走到这里 = 任务已是 succeeded：重复投递，直接返回既有状态。
                return
            self._assert_chunk_slots_free(current, chunks)
            # 核验对象是**持久化原文**，而且必须在写入之前（R4-05）：
            # 类型只保证长度对口，`content_hash` 只对片段自身取哈希 ——
            # 等长的伪内容两者都过得去。
            document = self._documents.get(current.document_id)
            if document is None:
                raise deny(
                    ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                    "任务对应的原文不在库里，无法核验片段来源",
                    job_id=current.job_id,
                )
            assert_chunks_match_document(current.document_id, document.content, chunks)
            for chunk in chunks:
                self._chunks[chunk.chunk_id] = chunk
                self._chunk_slots[(chunk.document_id, chunk.chunk_index)] = chunk.chunk_id
            self._jobs[job.job_id] = replace(
                current,
                status=IngestionStatus.SUCCEEDED,
                lease_owner="",
                lease_until=None,
                claim_token="",
                updated_at=self._clock.now(),
            )

    def fail(self, job: IngestionJob, *, error_code: str, safe_detail: str) -> None:
        with self._lock:
            current = self._jobs.get(job.job_id)
            if current is None:
                raise deny(
                    ErrorCode.CROSS_TENANT_DENIED, "任务不存在", job_id=job.job_id
                )
            if not self._holds_settlement_rights(current, job):
                self._reject_if_claim_is_stale(current, job, action="fail")
                # 走到这里 = 终态重复上报：幂等成功，**不覆盖**首次记录的错误。
                return
            self._jobs[job.job_id] = replace(
                current,
                status=IngestionStatus.FAILED,
                lease_owner="",
                lease_until=None,
                claim_token="",
                error_code=error_code,
                error_detail=safe_detail,
                updated_at=self._clock.now(),
            )

    # ------------------------------------------------------------------ 内部

    def _holds_settlement_rights(
        self, current: IngestionJob, claim: IngestionJob
    ) -> bool:
        """这次认领现在**仍然有权**落定吗（状态 + token + 未过期的租约）。

        三个条件缺一不可：

        - **状态**：`processing` 之外没有可落定的东西；
        - **token**：`lease_owner` 由运维提供（`--worker-id`），同一个进程重启后
          是同一个名字 —— "同一个名字"不等于"同一代持有者"，只有不可复用的
          token 才能分辨；
        - **期限**：过了期的持有者不该再落定任何东西，即使 token 还对。
        """
        return (
            current.status is IngestionStatus.PROCESSING
            and current.claim_token != ""
            and current.claim_token == claim.claim_token
            and current.lease_until is not None
            and self._clock.now() < current.lease_until
        )

    def _reject_if_claim_is_stale(
        self, current: IngestionJob, job: IngestionJob, *, action: str
    ) -> None:
        """条件落空时分辨两种情况，**只有当这次调用是重复投递时才正常返回**。

        名字刻意不是 `is_...` / `settled_already`：那种返回布尔的形状，
        调用方容易忘记看返回值而继续往下写 —— 本适配器就犯过一次
        （重复上报因此覆盖了首次记录的 `error_detail`，被契约测试抓下）。
        这里的形状是"**不抛异常就一定是重复投递**"，忘不掉。

        两种情况的处理必须相反：

        - 重复投递 → 正常返回（幂等成功）；
        - 这次认领已失效 → 抛冲突（绝不能改写新持有者的任务，
          而它可能正在被正常处理）。

        "哪种终态算重复投递"按动作分开，与 0007 的语义保持一致：

        - `complete`：只有 `succeeded` 算重复（结果已经在那里了）；
          已经是 `failed` 时**报冲突** —— 我们要写的是成功结果，而库里
          记着失败，静默返回会把这个矛盾藏起来。
        - `fail`：两种终态都算重复上报（worker 崩溃重放时会走到这里）。
        """
        if current.status is IngestionStatus.SUCCEEDED:
            return
        if action == "fail" and current.status is IngestionStatus.FAILED:
            return
        raise deny(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            "这次认领已经失效（租约过期后任务被重新认领）；"
            f"不得用 {action} 改写当前持有者的任务",
            job_id=job.job_id,
        )

    def _assert_chunk_slots_free(
        self, job: IngestionJob, chunks: tuple[StoredChunk, ...]
    ) -> None:
        for chunk in chunks:
            if chunk.chunk_id in self._chunks:
                raise deny(
                    ErrorCode.PARAMS_INVALID,
                    f"片段 id 已存在：{chunk.chunk_id}",
                    chunk_id=chunk.chunk_id,
                )
            slot = self._chunk_slots.get((chunk.document_id, chunk.chunk_index))
            if slot is not None:
                raise deny(
                    ErrorCode.PARAMS_INVALID,
                    f"文档 {chunk.document_id} 的片段序号 {chunk.chunk_index} 已存在",
                    job_id=job.job_id,
                )
