"""摄取仓储的 PostgreSQL 适配器（`knowledge.ports` 的数据库实现）。

四条纪律与 `db/product_store.py` 一致：参数绑定、事务边界即方法边界、
数据库错误翻译成平台错误、**每个方法的第一条语句之前先设 RLS 上下文**。

## 四个事务形状，各自服务一件事

| 方法 | 连接角色 | 上下文 | 为什么 |
|---|---|---|---|
| `enqueue` / `get_job` / `stored_chunks` | 应用（`study_app`） | 租户 + 项目 | 与其它项目级表一致的用户路径 |
| `claim_next` | **worker**（`study_worker`） | 无 | 见下 |
| `load_document` / `complete` / `fail` | **worker** | 任务自带的租户 + 项目 | 只能读取/落定认领范围内的原文 |

## 为什么 worker 路径是**另一个数据库角色**

worker 要先知道"哪个租户有活干"，才可能建立任何租户上下文。要求它带租户身份，
等于要求它在知道自己要处理谁之前就声称自己是谁 —— 那只能靠伪造身份来满足签名。

0007 给 `ingestion_jobs` 加了一条**独立的、有名有姓的**策略
（`ingestion_jobs_worker`，而不是往标准谓词里塞 `OR ...`：标准谓词读起来必须
与其它项目级表逐字一致，多出来的权限才有机会被单独看见）—— 但它**没有限定角色**。
于是 `app.worker_id` 这个自定义 GUC 成了事实上的凭据：任何持应用连接串的人
自己 `set_config` 一下就能读到全部队列（只读实测：0 行 → 177 行）。

0008 把策略限成 `TO study_worker`，并把 `UPDATE` 从应用角色收回；本模块对应地
把 worker 的四个方法改成走 `worker_dsn`。**两处必须同时成立**：只改迁移则 worker
认领不到任务（症状是"队列空了"），只改代码则越权依旧。

认领之后的一切写入仍走租户 + 项目上下文 —— worker 从任务行读出
`tenant_id` / `project_id` 再建立上下文，因此它拿不到认领范围之外的数据。

## 为什么认领是一条 SQL 而不是"先查后改"

「先查后改」是 check-then-act：两个 worker 会在查与改之间同时看到同一行，
于是同一份原文被处理两遍、写出两套片段。`FOR UPDATE SKIP LOCKED` 把
"选 + 占"压进一条语句 —— 被锁住的行对第二个 worker 直接不可见，它去拿下一条。

## 为什么读回来的行要核对 `content_hash`

`content_hash` 在 Python 契约里是**派生属性**（`sha256(content)`），
数据库列是同一条规则的物化副本。读回来的行如果两者不一致，说明
内容被改过而指纹没跟上 —— 那么所有引用、去重与校验都会以假指纹为真。
代价是每次读取都要重算一遍哈希（见 `_chunk_from_row`），
在当前规模下换的是"引用的可信"，值得。
"""

from __future__ import annotations

import json

from psycopg import errors as pg_errors

from app.core.artifacts import DisplayPolicy
from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.db.session import tenant_transaction, worker_transaction
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
from app.policy.taint import TaintSource

_DOCUMENT_COLUMNS = (
    "document_id, tenant_id, project_id, source_id, version, document_title,"
    " content, content_hash, media_type, language, parser_version,"
    " acquisition_method, taint_sources, derived_from, observed_at"
)

_JOB_COLUMNS = (
    "job_id, tenant_id, project_id, source_id, document_id, status, attempt_count,"
    " lease_owner, lease_until, claim_token, error_code, error_detail, created_at,"
    " updated_at"
)

_CHUNK_COLUMNS = (
    "chunk_id, tenant_id, project_id, source_id, document_id, chunk_index,"
    " heading_path, heading_level, span_start, span_end, content, content_hash,"
    " parser_version, display_policy, created_at"
)

#: 可认领的任务：排队中的，以及**租约已过期**的处理中任务（崩溃残留）。
#: 用 `SKIP LOCKED` 而不是普通 `FOR UPDATE`：被别的 worker 锁住的行直接跳过，
#: 而不是排队等它 —— 排队等待会让 worker 池串行化。
_CLAIM_SELECT = """
SELECT job_id FROM ingestion_jobs
 WHERE status = 'queued'
    OR (status = 'processing' AND lease_until < now())
 ORDER BY created_at, job_id
 FOR UPDATE SKIP LOCKED
 LIMIT 1
"""


def _document_from_row(row: tuple) -> SourceDocument:
    document = SourceDocument(
        document_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        source_id=row[3],
        version=row[4],
        document_title=row[5],
        content=row[6],
        media_type=row[8],
        language=row[9],
        parser_version=row[10],
        acquisition_method=row[11],
        taint_sources=tuple(TaintSource(item) for item in row[12]),
        derived_from=tuple(row[13]),
        observed_at=row[14],
    )
    _assert_hash_matches(row[7], document.content_hash, "source_documents", row[0])
    return document


def _job_from_row(row: tuple) -> IngestionJob:
    return IngestionJob(
        job_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        source_id=row[3],
        document_id=row[4],
        status=IngestionStatus(row[5]),
        attempt_count=row[6],
        lease_owner=row[7] or "",
        lease_until=row[8],
        # SQL 返回的是 uuid 对象，Python 契约里是字符串：空值（没有认领）读成空串。
        claim_token=str(row[9]) if row[9] is not None else "",
        error_code=row[10],
        error_detail=row[11],
        created_at=row[12],
        updated_at=row[13],
    )


def _chunk_from_row(row: tuple) -> StoredChunk:
    chunk = StoredChunk(
        chunk_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        source_id=row[3],
        document_id=row[4],
        chunk_index=row[5],
        heading_path=tuple(row[6]),
        heading_level=row[7],
        span_start=row[8],
        span_end=row[9],
        content=row[10],
        parser_version=row[12],
        # SQL 返回的是字符串，必须显式转枚举。这条"显式转换"只出现在读路径：
        # 构造契约时不做隐式转换（`require_enum` 的约定），否则"上游传了裸字符串"
        # 这个错误会被藏起来 —— 而在读路径上，字符串来自数据库自己，
        # 转换是必要的、也是唯一的入口。
        display_policy=DisplayPolicy(row[13]),
        created_at=row[14],
    )
    _assert_hash_matches(row[11], chunk.content_hash, "source_chunks", row[0])
    return chunk


def _assert_hash_matches(stored: str, derived: str, table: str, row_id: str) -> None:
    """库里的指纹必须与内容重算出来的指纹一致。

    不一致只有两种解释：内容被改过而指纹没跟上，或者写入方算错了哈希。
    两种都会让"引用可核验"变成一句空话 —— 而且是**静默地**变成空话，
    因为指纹本身就是校验的终点，没人会去质疑它。
    """
    if stored != derived:
        raise deny(
            ErrorCode.INTERNAL_CONSISTENCY_ERROR,
            f"{table} 行的 content_hash 与内容不一致；该行的引用不可信",
            row_id=row_id,
        )


def _jsonb_list(values: tuple) -> str:
    return json.dumps([str(item) for item in values], ensure_ascii=False)


class PostgresIngestionRepository:
    """原文 / 任务 / 片段的 PostgreSQL 实现。"""

    def __init__(
        self,
        *,
        membership: MembershipRepository,
        clock: Clock | None = None,
        dsn: str | None = None,
        worker_dsn: str | None = None,
    ) -> None:
        self.membership = membership
        self._clock = clock or SystemClock()
        #: 应用角色连接串（用户路径）。
        self._dsn = dsn
        #: worker 角色连接串（认领与落定）。见模块 docstring：
        #: 队列的跨租户可见性只授予该角色，两者不能互相顶替。
        self._worker_dsn = worker_dsn

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
        self.membership.get(actor, project_id)
        now = self._clock.now()
        document = SourceDocument(
            document_id=document_id,
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source_id,
            version=1,  # 占位：真实版本在事务内分配
            document_title=title,
            content=content,
            media_type=media_type,
            language=language,
            observed_at=now,
            parser_version=DOCUMENT_PARSER_VERSION,
            acquisition_method=ACQUISITION_METHOD_UPLOAD,
        )
        try:
            with tenant_transaction(
                tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
            ) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sources WHERE source_id = %s", (source_id,)
                ).fetchone()
                if exists is None:
                    # RLS 已按租户 + 项目过滤：查不到只剩"不属于本项目"一种解释。
                    raise deny(
                        ErrorCode.CROSS_TENANT_DENIED,
                        "无权访问该项目",
                        source_id=source_id,
                    )
                version_row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM source_documents"
                    " WHERE source_id = %s",
                    (source_id,),
                ).fetchone()
                assert version_row is not None, "聚合查询必返回一行（COALESCE 保证非 NULL）"
                document = _with_version(document, int(version_row[0]) + 1)
                conn.execute(
                    "INSERT INTO source_documents ("
                    + _DOCUMENT_COLUMNS
                    + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s::jsonb, %s::jsonb, %s)",
                    (
                        document.document_id,
                        document.tenant_id,
                        document.project_id,
                        document.source_id,
                        document.version,
                        document.document_title,
                        document.content,
                        document.content_hash,
                        document.media_type,
                        document.language,
                        document.parser_version,
                        document.acquisition_method,
                        _jsonb_list(document.taint_sources),
                        _jsonb_list(document.derived_from),
                        document.observed_at,
                    ),
                )
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
                conn.execute(
                    "INSERT INTO ingestion_jobs ("
                    " job_id, tenant_id, project_id, source_id, document_id, status,"
                    " attempt_count)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        job.job_id,
                        job.tenant_id,
                        job.project_id,
                        job.source_id,
                        job.document_id,
                        str(job.status),
                        job.attempt_count,
                    ),
                )
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.VERSION_CONFLICT,
                "该资料的版本号被并发上传占用；请重新提交（将基于最新版本分配）",
            ) from exc
        return document, job

    # ------------------------------------------------------------------ 读取

    def get_job(self, actor: Principal, project_id: str, job_id: str) -> IngestionJob:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _JOB_COLUMNS + " FROM ingestion_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        if row is None:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目", job_id=job_id
            )
        return _job_from_row(row)

    def list_jobs(self, actor: Principal, project_id: str) -> tuple[IngestionJob, ...]:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _JOB_COLUMNS
                + " FROM ingestion_jobs WHERE project_id = %s"
                " ORDER BY created_at DESC, job_id DESC",
                (project_id,),
            ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def load_document(self, job: IngestionJob) -> SourceDocument:
        with worker_transaction(
            tenant_id=job.tenant_id, project_id=job.project_id, dsn=self._worker_dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _DOCUMENT_COLUMNS
                + " FROM source_documents WHERE document_id = %s",
                (job.document_id,),
            ).fetchone()
        if row is None:
            raise deny(
                ErrorCode.CROSS_PROJECT_DENIED,
                "任务对应的原文不在该任务的作用域内",
                document_id=job.document_id,
            )
        return _document_from_row(row)

    def stored_chunks(
        self, actor: Principal, project_id: str, *, latest_only: bool = True
    ) -> tuple[StoredChunk, ...]:
        """该项目下的片段。`latest_only` 时每个来源只取最新版本（见端口契约）。

        「最新成功版本」由 `DISTINCT ON (source_id) ... ORDER BY version DESC`
        在**数据库内**确定：应用层先取全部再挑，会把历史版本的片段也拉回来
        （作用域虽然没错，但"该看见什么"的判断就跑到了应用层，而那是
        02 号规格 §7 明确要避免的形状）。
        """
        self.membership.get(actor, project_id)
        scope = ""
        if latest_only:
            scope = (
                " AND document_id IN ("
                " SELECT DISTINCT ON (d.source_id) d.document_id"
                " FROM source_documents d"
                " JOIN source_chunks x ON x.document_id = d.document_id"
                " WHERE d.project_id = %s"
                " ORDER BY d.source_id, d.version DESC)"
            )
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _CHUNK_COLUMNS
                + " FROM source_chunks WHERE project_id = %s"
                + scope
                + " ORDER BY document_id, chunk_index",
                (project_id, project_id) if latest_only else (project_id,),
            ).fetchall()
        return tuple(_chunk_from_row(row) for row in rows)

    def chunk_at(
        self,
        actor: Principal,
        project_id: str,
        *,
        document_id: str,
        span: tuple[int, int],
    ) -> StoredChunk | None:
        """按不可变标识精确回读（见端口契约：禁止"挑第一条"）。"""
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _CHUNK_COLUMNS
                + " FROM source_chunks"
                " WHERE project_id = %s AND document_id = %s"
                "   AND span_start = %s AND span_end = %s",
                (project_id, document_id, span[0], span[1]),
            ).fetchone()
        return _chunk_from_row(row) if row is not None else None

    # ------------------------------------------------------------------ 队列

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> IngestionJob | None:
        """认领下一个可做的任务（系统级操作，见模块 docstring）。

        连接用的是 **worker 角色**：队列的跨租户可见性在 0008 起限给
        `study_worker`，用应用角色连接会一条也看不到。
        """
        with worker_transaction(dsn=self._worker_dsn) as conn:
            conn.execute(
                "SELECT set_config('app.worker_id', %s, true)", (worker_id,)
            )
            selected = conn.execute(_CLAIM_SELECT).fetchone()
            if selected is None:
                return None
            row = conn.execute(
                "UPDATE ingestion_jobs"
                " SET status = 'processing',"
                "     attempt_count = attempt_count + 1,"
                "     lease_owner = %s,"
                "     lease_until = now() + make_interval(secs => %s),"
                "     claim_token = gen_random_uuid(),"
                "     updated_at = now()"
                " WHERE job_id = %s"
                " RETURNING " + _JOB_COLUMNS,
                (worker_id, float(lease_seconds), selected[0]),
            ).fetchone()
        assert row is not None, "刚被本事务锁住并选中的行不可能在 UPDATE 时消失"
        return _job_from_row(row)

    def complete(self, job: IngestionJob, chunks: tuple[StoredChunk, ...]) -> None:
        """落定成功。**必须持有仍然有效的认领**（token + 状态 + 租约期限）。

        时序上的失败模式（0009 之前存在，实测复现）：A 超时 → B 接管 →
        A 迟到的 `complete` 抢先落结果，B 写的片段与 A 的混在一起，
        而"谁写的"没有任何记录。所以落定不能只看"任务是不是 processing"，
        还要看"是不是**我这一代**在持有它、且租约还没过期"。
        """
        assert_chunks_belong_to_job(job, chunks)
        with worker_transaction(
            tenant_id=job.tenant_id, project_id=job.project_id, dsn=self._worker_dsn
        ) as conn:
            settled = conn.execute(
                "UPDATE ingestion_jobs"
                " SET status = 'succeeded', lease_owner = NULL, lease_until = NULL,"
                "     claim_token = NULL, updated_at = now()"
                " WHERE job_id = %s AND status = 'processing'"
                "   AND claim_token = %s AND lease_until > now()"
                " RETURNING job_id",
                (job.job_id, job.claim_token or None),
            ).fetchone()
            if settled is None:
                self._reject_if_claim_is_stale(conn, job, action="complete")
                # 走到这里 = 任务已是 succeeded：重复投递，直接返回既有结果。
                return
            # 核验对象是**持久化原文**，而且在**同一个事务、写入之前**（R4-05）：
            # 类型只保证长度对口、`content_hash` 只对片段自身取哈希 ——
            # 等长的伪内容两者都过得去（实测 `alphabet` → `XXXXXXXX` 一路通过）。
            # 读的是库里的那一行，不是调用方手里那份对象。
            stored_text = conn.execute(
                "SELECT content FROM source_documents WHERE document_id = %s",
                (job.document_id,),
            ).fetchone()
            if stored_text is None:
                raise deny(
                    ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                    "任务对应的原文不在库里，无法核验片段来源",
                    job_id=job.job_id,
                )
            assert_chunks_match_document(job.document_id, stored_text[0], chunks)
            for chunk in chunks:
                conn.execute(
                    "INSERT INTO source_chunks ("
                    + _CHUNK_COLUMNS
                    + ") VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,"
                    " %s, %s, %s, %s)",
                    (
                        chunk.chunk_id,
                        chunk.tenant_id,
                        chunk.project_id,
                        chunk.source_id,
                        chunk.document_id,
                        chunk.chunk_index,
                        _jsonb_list(chunk.heading_path),
                        chunk.heading_level,
                        chunk.span_start,
                        chunk.span_end,
                        chunk.content,
                        chunk.content_hash,
                        chunk.parser_version,
                        str(chunk.display_policy),
                        chunk.created_at,
                    ),
                )

    def fail(self, job: IngestionJob, *, error_code: str, safe_detail: str) -> None:
        """标为失败。与 `complete` 同一套围栏：**失去租约的一方不得改写
        新持有者的任务**。

        ⚠️ 这条尤其重要：`fail` 写的是终态。若旧持有者能迟到地把 B 正在处理的
        任务打成 `failed`，可恢复的状态就被变成了不可恢复 —— 比"不处理"更糟。
        """
        with worker_transaction(
            tenant_id=job.tenant_id, project_id=job.project_id, dsn=self._worker_dsn
        ) as conn:
            settled = conn.execute(
                "UPDATE ingestion_jobs"
                " SET status = 'failed', lease_owner = NULL, lease_until = NULL,"
                "     claim_token = NULL, error_code = %s, error_detail = %s,"
                "     updated_at = now()"
                " WHERE job_id = %s AND status = 'processing'"
                "   AND claim_token = %s AND lease_until > now()"
                " RETURNING job_id",
                (error_code, safe_detail, job.job_id, job.claim_token or None),
            ).fetchone()
            if settled is None:
                self._reject_if_claim_is_stale(conn, job, action="fail")

    def _reject_if_claim_is_stale(self, conn, job: IngestionJob, *, action: str) -> None:
        """条件更新落空时，分清"幂等重放"与"这次认领已经失效"。

        名字刻意不是 `is_...` / `settled_already`：返回布尔的形状容易让调用方
        忘记看返回值而继续往下写（内存适配器就犯过一次 —— 重复上报因此覆盖了
        首次记录的 `error_detail`，被契约测试抓下）。这里的形状是
        "**不抛异常就一定是重复投递**"。

        两种情况的处理**必须相反**：

        - **重复投递**：worker 写完结果、还没确认就被杀，消息重新投递。
          第二次落定必须**成功返回**，否则重投永远失败。
        - **认领失效**（仍是 processing，但 token 不是我的）：租约过期后
          任务被别的 worker 接管了。此时必须报冲突，绝不能改写对方的状态。

        "哪种终态算重复投递"按动作分开，与 0007 的语义保持一致：

        - `complete`：只有 `succeeded` 算重复（结果已经在那里了）；
          已经是 `failed` 时报冲突 —— 我们要写的是成功结果，而库里记着失败，
          静默返回会把这个矛盾藏起来。
        - `fail`：两种终态都算重复上报（worker 崩溃重放时会走到这里）。

        0007 的实现把"幂等重放"与"认领失效"混在"状态不是 processing 就抛错 /
        是 succeeded 就返回"两个分支里，恰好把第二种情况漏成了
        "只要状态还是 processing 就照写不误"。

        ⚠️ **已知且刻意的边界**：任务已经被别人落定成 `succeeded` 之后，
        旧持有者的迟到 `complete` 会得到幂等成功，而不是冲突 —— token 在落定时
        被清空，事后无法区分"我做的"与"别人做的"。这不改变任何状态、
        也不会重复写片段（唯一键还在），代价只是旧持有者不知道自己的结果被丢弃。
        要消除这个代价就得让 token 永久保留，而那是另一个设计
        （引用需要一个不可变的落定记录，属于后续轮次）。
        """
        current = conn.execute(
            "SELECT status FROM ingestion_jobs WHERE job_id = %s FOR UPDATE",
            (job.job_id,),
        ).fetchone()
        if current is None:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED, "任务不存在", job_id=job.job_id
            )
        if current[0] == str(IngestionStatus.SUCCEEDED):
            return
        if action == "fail" and current[0] == str(IngestionStatus.FAILED):
            return
        raise deny(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            "这次认领已经失效（租约过期后任务被重新认领）；"
            f"不得用 {action} 改写当前持有者的任务",
            job_id=job.job_id,
        )


def _with_version(document: SourceDocument, version: int) -> SourceDocument:
    from dataclasses import replace

    return replace(document, version=version)
