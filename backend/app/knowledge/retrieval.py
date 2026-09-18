"""知识检索（开发适配器）。

⚠️ **降级声明**：本版是进程内关键词匹配，**不是**设计中的混合检索管线
   （中文分词 + `tsvector` + pgvector + 融合 + rerank + 证据充分性分析）。
   生产适配器未实现。

有两点语义**不能**因为简化而省略：

1. 租户与项目过滤在**检索内部**强制 —— 不能先把跨租户候选拉上来再在应用层筛
   （02 号规格 §7 明确禁止）；
2. 检索结果一律携带 taint 与来源引用（`source_id` / `span` / `content_hash`），
   否则引用装配与展示策略都无从判定。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.artifacts import ArtifactRef, DisplayPolicy
from app.policy.taint import TaintSource, TaintedValue, mark_tainted
from app.tenancy.context import current


@dataclass(frozen=True)
class Chunk:
    """一个可检索片段。属于某个项目，来源可追溯。"""

    chunk_id: str
    tenant_id: str
    learning_project_id: str
    source_id: str
    span: tuple[int, int]
    text: str
    parser_version: str = "parser/v1"
    display_policy: DisplayPolicy = DisplayPolicy.FULL
    origin: TaintSource = TaintSource.UPLOADED_SOURCE

    def artifact_ref(self) -> ArtifactRef:
        """转成谱系引用。内容本体不进上下文，只带指针与指纹。"""
        value = mark_tainted(self.chunk_id, self.text, self.origin)
        return ArtifactRef(
            source_id=self.source_id,
            span=self.span,
            content_hash=value.content_hash,
            parser_version=self.parser_version,
            display_policy=self.display_policy,
        )


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: int

    def as_tainted(self) -> TaintedValue:
        """检索结果天然是外部内容，一律带 taint。"""
        return mark_tainted(self.chunk.chunk_id, self.chunk.text, self.chunk.origin)


class ChunkIndex:
    """进程内片段索引。租户与项目过滤固化在查询路径里。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []

    def add(self, chunk: Chunk) -> None:
        """写入片段。租户必须与当前上下文一致。"""
        context = current()
        if chunk.tenant_id != context.tenant_id:
            from app.core.errors import ErrorCode, deny

            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "摄取片段所属租户与当前上下文不一致",
                expected=context.tenant_id,
                actual=chunk.tenant_id,
            )
        self._chunks.append(chunk)

    def search(self, query: str, *, limit: int = 5) -> list[ScoredChunk]:
        """按词命中数打分。

        过滤在**内部**完成：先按租户、再按项目，之后才打分排序。
        """
        context = current()
        project_id = context.require_project()
        terms = [t for t in _tokenize(query) if t]
        scored: list[ScoredChunk] = []
        for chunk in self._chunks:
            if chunk.tenant_id != context.tenant_id:
                continue
            if chunk.learning_project_id != project_id:
                continue
            text = chunk.text.lower()
            score = sum(1 for term in terms if term in text)
            if score > 0:
                scored.append(ScoredChunk(chunk=chunk, score=score))
        scored.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return scored[:limit]


def _tokenize(query: str) -> list[str]:
    """极简分词：按空白与非字母数字切分。

    真实实现需要中文分词器并在自有语料上做基准（02 号规格 §3），此处从简。
    """
    import re

    return [t for t in re.split(r"[\s\W_]+", query.lower()) if t]
