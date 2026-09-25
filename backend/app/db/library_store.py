"""用户级知识库的 PostgreSQL 适配器（`knowledge.library` 的数据库实现）。

三条纪律与 `db.product_store.py` 一致：参数绑定、事务边界即方法边界、
数据库错误翻译成平台错误。

## 为什么走 `principal_transaction`

这两张表的策略是「租户 + 主体」（`0021` 迁移），缺 `app.principal_id`
时 `principal_id = NULL` 恒假 —— 查不到任何行。主体上下文来自已验签的会话
声明，客户端无法通过请求字段改变它，因此"按主体隔离"不是应用层记得
传对参数，而是数据库执行的不变量。

## `has_content` 由查询算出来

记录**没有** `has_content` 列：它由 `library_documents` 是否有行推导
（`EXISTS` 子查询，与 RLS 同一事务上下文）。存成一个布尔列就得成对更新，
而"库里说有原文、其实没有"这种不一致没有任何一处能报出来。
"""

from __future__ import annotations

import json

from psycopg import errors as pg_errors

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.db.session import principal_transaction
from app.identity.models import Principal
from app.knowledge.library import LibraryDocument, LibrarySource

#: 记录 + 派生的 `has_content`。子查询与主查询同一事务上下文，
#: 因此它同样受 RLS 约束 —— 不可能读到别人主体的原文行。
_SOURCE_SELECT = """
SELECT s.library_source_id, s.tenant_id, s.principal_id, s.display_name,
       s.media_type, s.identity_hash, s.registered_at, s.acquisition,
       EXISTS (
           SELECT 1 FROM library_documents d
            WHERE d.library_source_id = s.library_source_id
       ) AS has_content
  FROM library_sources s
"""

_DOCUMENT_INSERT = (
    "INSERT INTO library_documents (library_document_id, tenant_id, principal_id,"
    " library_source_id, version, content, content_hash, parser_version, observed_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _source_from_row(row: tuple) -> LibrarySource:
    return LibrarySource(
        library_source_id=row[0],
        tenant_id=row[1],
        principal_id=row[2],
        display_name=row[3],
        media_type=row[4],
        identity_hash=row[5],
        registered_at=row[6],
        acquisition=row[7] or {},
        has_content=bool(row[8]),
    )


def _jsonb(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class PostgresLibraryRepository:
    """知识库记录与库内原文的 PostgreSQL 实现。"""

    def __init__(self, *, clock: Clock | None = None, dsn: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._dsn = dsn

    # ------------------------------------------------------------------ 写入

    def register(
        self,
        actor: Principal,
        *,
        library_source_id: str,
        display_name: str,
        media_type: str,
        identity_hash: str,
        acquisition: dict,
        content: str = "",
        library_document_id: str = "",
    ) -> tuple[LibrarySource, bool]:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            inserted = conn.execute(
                "INSERT INTO library_sources (library_source_id, tenant_id,"
                " principal_id, display_name, media_type, identity_hash, acquisition)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)"
                " ON CONFLICT (tenant_id, principal_id, identity_hash) DO NOTHING"
                " RETURNING library_source_id",
                (
                    library_source_id,
                    actor.tenant_id,
                    actor.principal_id,
                    display_name,
                    media_type,
                    identity_hash,
                    _jsonb(acquisition),
                ),
            ).fetchone()
            created = inserted is not None
            if inserted is not None:
                stored_id = inserted[0]
                if content:
                    self._insert_document(
                        conn,
                        actor,
                        stored_id,
                        content=content,
                        library_document_id=library_document_id,
                    )
            else:
                row = conn.execute(
                    "SELECT library_source_id FROM library_sources"
                    " WHERE tenant_id = %s AND principal_id = %s AND identity_hash = %s",
                    (actor.tenant_id, actor.principal_id, identity_hash),
                ).fetchone()
                assert row is not None, "唯一键冲突必然对应一条既有记录"
                stored_id = row[0]
            return self._read_source(conn, stored_id), created

    def set_content(
        self,
        actor: Principal,
        library_source_id: str,
        *,
        content: str,
        library_document_id: str,
    ) -> LibrarySource:
        try:
            with principal_transaction(
                tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
            ) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM library_sources WHERE library_source_id = %s",
                    (library_source_id,),
                ).fetchone()
                if exists is None:
                    # RLS 已按租户 + 主体过滤：查不到只剩"不属于我/不存在"一种解释。
                    raise deny(
                        ErrorCode.CROSS_TENANT_DENIED,
                        "资源不存在",
                        library_source_id=library_source_id,
                    )
                self._insert_document(
                    conn,
                    actor,
                    library_source_id,
                    content=content,
                    library_document_id=library_document_id,
                )
                return self._read_source(conn, library_source_id)
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.VERSION_CONFLICT,
                "库内原文的版本号被并发写入占用；请重新提交（将基于最新版本分配）",
            ) from exc

    # ------------------------------------------------------------------ 读取

    def list_sources(self, actor: Principal) -> tuple[LibrarySource, ...]:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                _SOURCE_SELECT + " ORDER BY s.registered_at, s.library_source_id"
            ).fetchall()
        return tuple(_source_from_row(row) for row in rows)

    def get_source(self, actor: Principal, library_source_id: str) -> LibrarySource:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                _SOURCE_SELECT + " WHERE s.library_source_id = %s",
                (library_source_id,),
            ).fetchone()
        if row is None:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED, "资源不存在", library_source_id=library_source_id
            )
        return _source_from_row(row)

    def latest_content(self, actor: Principal, library_source_id: str) -> str | None:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT content FROM library_documents"
                " WHERE library_source_id = %s ORDER BY version DESC LIMIT 1",
                (library_source_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def latest_document_version(self, actor: Principal, library_source_id: str) -> int | None:
        with principal_transaction(
            tenant_id=actor.tenant_id, principal_id=actor.principal_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT version FROM library_documents"
                " WHERE library_source_id = %s ORDER BY version DESC LIMIT 1",
                (library_source_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    # ------------------------------------------------------------------ 内部

    def _insert_document(
        self,
        conn,
        actor: Principal,
        library_source_id: str,
        *,
        content: str,
        library_document_id: str,
    ) -> None:
        """调用方必须已在租户 + 主体事务内，且资料可见。版本 = 当前最大 + 1。"""
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM library_documents"
            " WHERE library_source_id = %s",
            (library_source_id,),
        ).fetchone()
        assert version_row is not None, "聚合查询必返回一行（COALESCE 保证非 NULL）"
        document = LibraryDocument(
            library_document_id=library_document_id,
            tenant_id=actor.tenant_id,
            principal_id=actor.principal_id,
            library_source_id=library_source_id,
            version=int(version_row[0]) + 1,
            content=content,
            observed_at=self._clock.now(),
        )
        conn.execute(
            _DOCUMENT_INSERT,
            (
                document.library_document_id,
                document.tenant_id,
                document.principal_id,
                document.library_source_id,
                document.version,
                document.content,
                document.content_hash,
                document.parser_version,
                document.observed_at,
            ),
        )

    def _read_source(self, conn, library_source_id: str) -> LibrarySource:
        row = conn.execute(
            _SOURCE_SELECT + " WHERE s.library_source_id = %s", (library_source_id,)
        ).fetchone()
        assert row is not None, "同一事务内刚写入/读到的记录必然可见"
        return _source_from_row(row)
