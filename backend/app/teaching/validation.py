"""provider 返回结果的校验：引用核验与 grounding 判定。

## 校验证明什么、不证明什么

通过校验的引用证明两件事，**且仅此两件**：

1. 它指向本次已授权快照里的**那一版**原文（伪造 / 跨项目 / 未知版本
   都在这里被拒）；
2. 原文回读与 hash 仍然一致（快照构建与校验之间原文没有被换版）。

它**不**证明"结论得到了资料支持" —— 那是语义评估，本轮没有独立的
评估器，所以证据状态维持保守（`assess` 恒为 `insufficient`），
`grounding` 只回答"有没有引用活下来"，不回答"说得对不对"。

## 一次生成，不自动重试

校验失败不触发重新生成（那会变成"模型学会迎合校验器"的过拟合循环，
而且每次重试都是真实的费用）。首版语义：一次生成，失败引用逐条记录，
活下来的引用决定 grounding；全部被拒时回答以 `inference_only` 落定
—— 它仍然是一条有效的回答，只是**没有**资料背书。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.knowledge.store import KnowledgeRepository
from app.teaching.context import RetrievalSnapshot
from app.teaching.models import RawCitation
from app.teaching.runs import Grounding

#: 引用被拒的稳定原因（闭集：会进事件 payload，客户端可见）。
REJECT_NOT_IN_SNAPSHOT = "not_in_snapshot"
REJECT_SPAN_MISMATCH = "span_mismatch"
REJECT_HASH_MISMATCH = "hash_mismatch"
REJECT_READBACK_FAILED = "readback_failed"


@dataclass(frozen=True)
class CitationValidation:
    """校验结果：活下来的引用（ArtifactRef 形状）+ 被拒清单。"""

    accepted: tuple[dict, ...]
    rejected: tuple[tuple[RawCitation, str], ...]

    @property
    def grounding(self) -> Grounding:
        return Grounding.SOURCED if self.accepted else Grounding.INFERENCE_ONLY


def validate_citations(
    actor: Principal,
    project_id: str,
    raw_citations: tuple[RawCitation, ...],
    *,
    snapshot: RetrievalSnapshot,
    knowledge: KnowledgeRepository,
) -> CitationValidation:
    """逐条核验 provider 返回的引用。

    三道关卡（各自拒绝一种"看起来正常"的错）：

    1. **快照子集**：`(source_id, document_id, span_start, span_end,
       content_hash)` 五元组必须逐字段命中快照里的某个片段 ——
       编造的 source_id、拼凑的 document_id、别的项目的资料全部拒之门外；
    2. **回读**：按不可变标识从存储精确取回那一版原文
       （`knowledge.read_span`，取不到 = 引用悬空）；
    3. **内容一致**：回读到的片段内容必须与快照里的一致
       （防御"快照之后原文被换版"的窗口）。

    `read_span` 拿不到时是"落空"不是异常 —— 但调用方传入的
    `actor` 保证它不会越权读到别的项目的内容（隔离在存储层）。
    """
    by_key: dict[tuple, dict] = {
        (
            item.source_id,
            item.document_id,
            item.span_start,
            item.span_end,
            item.content_hash,
        ): item.as_citation_dict()
        for item in snapshot.items
    }
    accepted: list[dict] = []
    rejected: list[tuple[RawCitation, str]] = []
    seen: set[tuple] = set()
    for citation in raw_citations:
        key = (
            citation.source_id,
            citation.document_id,
            citation.span_start,
            citation.span_end,
            citation.content_hash,
        )
        entry = by_key.get(key)
        if entry is None:
            # 区分两种拒绝原因便于排障：跨度对不上 vs 整体陌生。
            near = any(
                item.source_id == citation.source_id
                and item.document_id == citation.document_id
                and item.span_start == citation.span_start
                and item.span_end == citation.span_end
                for item in snapshot.items
            )
            reason = REJECT_HASH_MISMATCH if near else REJECT_NOT_IN_SNAPSHOT
            rejected.append((citation, reason))
            continue
        if key in seen:
            continue  # 重复引用去重：同一段证据引用两次只收一次
        try:
            chunk = knowledge.read_span(
                actor,
                project_id,
                citation.source_id,
                (citation.span_start, citation.span_end),
                document_id=citation.document_id,
                content_hash=citation.content_hash,
            )
            stored = chunk.content if chunk is not None else None
        except PlatformError:
            # 归属判定拒绝（actor 根本无权访问那个项目/来源）：同样算
            # "回读失败" —— 引用没有活路，但它不该让整次校验崩掉。
            stored = None
        if stored is None or stored != by_key_item(snapshot, key):
            rejected.append((citation, REJECT_READBACK_FAILED))
            continue
        accepted.append(entry)
        seen.add(key)
    return CitationValidation(accepted=tuple(accepted), rejected=tuple(rejected))


def by_key_item(snapshot: RetrievalSnapshot, key: tuple) -> str:
    """快照里命中键的片段内容（回读一致性比对用）。"""
    source_id, document_id, span_start, span_end, content_hash = key
    for item in snapshot.items:
        if (
            item.source_id == source_id
            and item.document_id == document_id
            and item.span_start == span_start
            and item.span_end == span_end
            and item.content_hash == content_hash
        ):
            return item.content
    raise PlatformError(  # pragma: no cover - 调用方已确保命中
        ErrorCode.INTERNAL_CONSISTENCY_ERROR, "快照键未命中"
    )
