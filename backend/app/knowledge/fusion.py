"""确定性 RRF 融合（R9 任务 3）。

## 为什么用 RRF 而不是分数相加

关键词分数是整数加法（`retrieval.py`），向量相似度是 `[-1, 1]` 的
浮点余弦 —— 两个量纲**不可比**。任何"归一化后加权"的方案都需要一个
校准过的权重，而本轮没有标注集支撑校准。RRF（Reciprocal Rank Fusion）
只用**排名**，不用分数：

```text
rrf(d) = Σ  1 / (k + rank_i(d))    对每个产生候选的列表 i
```

排名是整数，`k` 是常数，因此每个候选的融合分数是**精确有理数**
（`fractions.Fraction`），排序在任何平台上逐位一致 —— 与
`retrieval.py` 的"整数加法保确定性"是同一条纪律的延续。

## tie-break 为什么用 `(source_id, chunk_index)`

同分候选必须稳定排序。`chunk_id` 是随机生成的，拿它当次序等于
"同分时随机排"；这与 `rank_chunks` 的 tie-break 规则保持一致，
两个列表里的同一片段才能在任何重放下得到同一融合结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from app.knowledge.models import StoredChunk
from app.knowledge.retrieval import ScoredChunk

#: RRF 平滑常数。60 是文献与工业实现的常用值：它让 rank 1 与 rank 2
#: 的贡献差距足够小（1/61 vs 1/62），单列表霸榜压不过双列表都命中；
#: 又没有小到让排名失去意义。**改这个值 = 改排序规则**，必须同步
#: 更新 `HYBRID_RANKING_VERSION` 并重跑冻结评测集。
RRF_K = 60

#: 混合检索排序规则的版本。与 `RANKING_VERSION`（`keyword/v1`）平行：
#: 融合规则、RRF_K 或候选生成逻辑任一变化都必须改它 —— 版本没变而
#: 结果变了，说明有人在没意识到的情况下改了排序。
HYBRID_RANKING_VERSION = "hybrid-rrf/v1"

#: 召回来源的闭集标签。
SOURCE_KEYWORD = "keyword"
SOURCE_VECTOR = "vector"


@dataclass(frozen=True)
class FusedCandidate:
    """一条融合后的候选：片段 + 两路各自的排名与证据。

    `keyword_evidence` 保留关键词侧的分数与命中项（向量列表里的候选
    它是 `None`）：融合决定**顺序**，而"为什么命中"的证据来自各自
    的召回路径 —— 丢了它，排障时"这条为什么在第四位"就只能靠重跑。
    """

    chunk: StoredChunk
    keyword_rank: int | None
    vector_rank: int | None
    keyword_evidence: ScoredChunk | None

    @property
    def rrf_score(self) -> Fraction:
        """精确有理数融合分。任一列表未命中该候选时该项贡献为 0。"""
        score = Fraction(0)
        if self.keyword_rank is not None:
            score += Fraction(1, RRF_K + self.keyword_rank)
        if self.vector_rank is not None:
            score += Fraction(1, RRF_K + self.vector_rank)
        return score

    @property
    def recalled_from(self) -> tuple[str, ...]:
        """召回来源（有序闭集标签）。`both` 不是单独的值 ——
        两个来源各自在场，统计时才不会把"双路命中"折叠成第三类。"""
        sources: list[str] = []
        if self.keyword_rank is not None:
            sources.append(SOURCE_KEYWORD)
        if self.vector_rank is not None:
            sources.append(SOURCE_VECTOR)
        return tuple(sources)

    def as_scored_chunk(self) -> ScoredChunk:
        """转回关键词基线的候选形状。

        融合列表的**顺序**已经是 RRF 的结论；这里的 `score` 只是
        关键词侧证据的搬运（向量候选为 0），供沿用 `ScoredChunk`
        接口的调用方（快照构建、证据判定）继续工作 —— 不让混合检索
        的引入迫使整条下游链路换类型。
        """
        if self.keyword_evidence is not None:
            return self.keyword_evidence
        return ScoredChunk(chunk=self.chunk, score=0, matched_terms=())


def fuse_rrf(
    keyword_hits: tuple[ScoredChunk, ...],
    vector_hits: tuple[tuple[StoredChunk, int], ...],
    *,
    limit: int,
) -> tuple[FusedCandidate, ...]:
    """合并关键词与向量两个候选列表，返回前 `limit` 条。

    `vector_hits` 的每个元素是 `(片段, 向量排名)`：排名由索引侧给出
    （相似度降序的位置），本函数不重新计算 —— 融合只消费排名，
    这让"排名怎么来的"只有一个答案。

    纯函数：同两个列表必然得到同一融合结果。
    """
    if limit <= 0:
        return ()
    by_chunk: dict[str, FusedCandidate] = {}
    for position, hit in enumerate(keyword_hits, start=1):
        by_chunk[hit.chunk.chunk_id] = FusedCandidate(
            chunk=hit.chunk,
            keyword_rank=position,
            vector_rank=None,
            keyword_evidence=hit,
        )
    for chunk, rank in vector_hits:
        existing = by_chunk.get(chunk.chunk_id)
        if existing is None:
            by_chunk[chunk.chunk_id] = FusedCandidate(
                chunk=chunk,
                keyword_rank=None,
                vector_rank=rank,
                keyword_evidence=None,
            )
        else:
            by_chunk[chunk.chunk_id] = FusedCandidate(
                chunk=existing.chunk,
                keyword_rank=existing.keyword_rank,
                vector_rank=rank,
                keyword_evidence=existing.keyword_evidence,
            )
    candidates = tuple(by_chunk.values())
    candidates = tuple(
        sorted(
            candidates,
            key=lambda item: (-item.rrf_score, item.chunk.source_id, item.chunk.chunk_index),
        )
    )
    return candidates[:limit]
