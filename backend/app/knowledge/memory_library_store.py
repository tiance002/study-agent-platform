"""用户级知识库的内存实现（开发适配器）。

⚠️ 与 `product/memory_store.py` 一样，这是**开发适配器**：没有 RLS，
隔离靠本类的判定；PostgreSQL 实现由 FORCE RLS 兜底。两者跑同一套契约测试。

三条不能因为"内存版简单"就省掉的语义：

1. **按 `(tenant, principal, identity_hash)` 幂等**：重复登记返回既有记录，
   不产生第二条 —— 与 PostgreSQL 的 `UNIQUE (tenant_id, principal_id, identity_hash)`
   同一语义。
2. **主体隔离**：任何读写都先确认记录的 `tenant_id`/`principal_id` 与调用者一致，
   否则抛**同一个**拒绝码（不可见与不存在不可区分）。
3. **`has_content` 是派生事实**：由库内原文是否存在算出来，
   不存成一个可以自己变的开关。
"""

from __future__ import annotations

import threading
from dataclasses import replace

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.identity.models import Principal
from app.knowledge.library import LibraryDocument, LibrarySource


class InMemoryLibraryRepository:
    """知识库记录与库内原文的内存存储。

    两个字典共用一把 `RLock`：`register` 与 `set_content` 都同时读
    "有没有既有记录/版本"并写入，各自的锁只会让人以为"已经安全了"。
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._sources: dict[str, LibrarySource] = {}
        self._documents: dict[str, list[LibraryDocument]] = {}

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
        with self._lock:
            for existing in self._sources.values():
                if (
                    existing.tenant_id == actor.tenant_id
                    and existing.principal_id == actor.principal_id
                    and existing.identity_hash == identity_hash
                ):
                    # 幂等成功：返回既有记录。**不**用本次的 content 覆盖。
                    return self._view(existing), False
            source = LibrarySource(
                library_source_id=library_source_id,
                tenant_id=actor.tenant_id,
                principal_id=actor.principal_id,
                display_name=display_name,
                media_type=media_type,
                identity_hash=identity_hash,
                registered_at=self._clock.now(),
                acquisition=acquisition,
            )
            self._sources[library_source_id] = source
            if content:
                self._append_document(
                    actor,
                    library_source_id,
                    content=content,
                    library_document_id=library_document_id,
                )
            return self._view(source), True

    def list_sources(self, actor: Principal) -> tuple[LibrarySource, ...]:
        with self._lock:
            rows = [
                self._view(source)
                for source in self._sources.values()
                if source.tenant_id == actor.tenant_id
                and source.principal_id == actor.principal_id
            ]
        return tuple(sorted(rows, key=lambda row: (row.registered_at, row.library_source_id)))

    def get_source(self, actor: Principal, library_source_id: str) -> LibrarySource:
        with self._lock:
            source = self._sources.get(library_source_id)
        if (
            source is None
            or source.tenant_id != actor.tenant_id
            or source.principal_id != actor.principal_id
        ):
            # 不可见与不存在同码同话术：区分原因等于提供存在性探针。
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "资源不存在", library_source_id=library_source_id)
        return self._view(source)

    def latest_content(self, actor: Principal, library_source_id: str) -> str | None:
        with self._lock:
            source = self._sources.get(library_source_id)
            if (
                source is None
                or source.tenant_id != actor.tenant_id
                or source.principal_id != actor.principal_id
            ):
                return None
            documents = self._documents.get(library_source_id, [])
            if not documents:
                return None
            return max(documents, key=lambda row: row.version).content

    def latest_document_version(self, actor: Principal, library_source_id: str) -> int | None:
        with self._lock:
            source = self._sources.get(library_source_id)
            if (
                source is None
                or source.tenant_id != actor.tenant_id
                or source.principal_id != actor.principal_id
            ):
                return None
            documents = self._documents.get(library_source_id, [])
            if not documents:
                return None
            return max(row.version for row in documents)

    def set_content(
        self,
        actor: Principal,
        library_source_id: str,
        *,
        content: str,
        library_document_id: str,
    ) -> LibrarySource:
        # 先做访问判定（与 get_source 同源）：不可见时抛同一个拒绝码。
        self.get_source(actor, library_source_id)
        with self._lock:
            self._append_document(
                actor,
                library_source_id,
                content=content,
                library_document_id=library_document_id,
            )
            return self._view(self._sources[library_source_id])

    # ------------------------------------------------------------------ 内部

    def _append_document(
        self,
        actor: Principal,
        library_source_id: str,
        *,
        content: str,
        library_document_id: str,
    ) -> None:
        """调用方必须持有 ``_lock``。版本 = 当前最大 + 1。"""
        documents = self._documents.setdefault(library_source_id, [])
        document = LibraryDocument(
            library_document_id=library_document_id,
            tenant_id=actor.tenant_id,
            principal_id=actor.principal_id,
            library_source_id=library_source_id,
            version=max((row.version for row in documents), default=0) + 1,
            content=content,
            observed_at=self._clock.now(),
        )
        documents.append(document)

    def _view(self, source: LibrarySource) -> LibrarySource:
        """把"库里有没有原文"算进返回视图，而不是存成一个开关。"""
        return replace(source, has_content=bool(self._documents.get(source.library_source_id)))
