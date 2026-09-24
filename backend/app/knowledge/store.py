"""项目内检索：**作用域收窄 + 确定性排序 + 保守的证据判定**。

## 为什么只有一份实现（没有第二个 "PostgresKnowledgeRepository"）

计划里写的是"PostgreSQL 知识适配器"。真做出来会发现：检索这一层**没有任何
后端相关逻辑** —— 它是纯 Python 的整数打分，输入是"已经按租户/项目收窄的片段"。
按后端分叉的其实是**隔离**，而那一层已经住在 `IngestionRepository` 里
（内存版靠类内判定，PostgreSQL 版靠 FORCE RLS + 组合外键）。

硬要写第二份实现，只能把同一段排序代码再抄一遍，于是"两个适配器行为一致"
从"结构相同"退化成"两份代码碰巧一样" —— 后者正是本项目一直在拆的那种隐患。
契约测试因此在两个 `IngestionRepository` 实现上各跑一遍**同一套检索断言**。

## 候选集必须先收窄，再打分

`search` 的第一步是 `ingestion.stored_chunks(actor, project_id)` —— 作用域过滤
发生在 SQL / RLS 里，**在打分之前**。02 号规格 §7 明确禁止"把跨项目候选拉回
应用层再筛"：那样即使筛对了，越界内容也已经离开了它该在的边界，
而任何一次筛漏都会变成静默的越权。

它同时默认 `latest_only=True`：同一来源**只检索最新成功版本**。
历史版本的片段必须留在库里（老引用要按 `document_id` 回读原版），
但让检索同时看到两版会返回两份近似结果，而"哪份是当前的"客户端无从判断。

## 为什么不在 SQL 里预筛关键词

可以，但那会引入一个**与打分不同的语义**：SQL 的 `ILIKE` 与这里的 NFKC 归一 +
2-gram 不可能永远一致，于是"预筛掉了、打分本来会给分"的候选会静默消失 ——
召回漏洞没有任何报错。基线阶段宁可把全部项目内片段取回来打分（单版原文上限
1 MiB，量级可控），等规模上来时再换成"预筛 + 打分同源"的做法，
而不是现在留一个看不出来的召回缺口。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.evidence_issues import EvidenceAssessment
from app.identity.models import Principal
from app.knowledge.embedding import (
    EmbeddingProvider,
    VectorIndex,
    embed_chunk_key,
)
from app.knowledge.evidence_state import (
    RetrievalHealth,
    RetrievalSignals,
    assess_retrieval,
    assess_retrieval_health,
)
from app.knowledge.fusion import HYBRID_RANKING_VERSION, fuse_rrf
from app.knowledge.models import StoredChunk
from app.knowledge.ports import IngestionRepository
from app.knowledge.retrieval import RANKING_VERSION, RELEVANCE_FLOOR, ScoredChunk, rank_chunks

#: 检索模式闭集（R9 任务 3 / 任务 6 的指标标签）。
#:
#: - `keyword`：向量组件未注入，走 `keyword/v1` 基线 —— 这是**正常**状态，
#:   不是降级；
#: - `hybrid`：关键词与向量两路候选经确定性 RRF 融合；
#: - `degraded`：配置了向量组件但失败回退关键词。降级必须携带原因
#:   （`DEGRADED_REASONS`），否则"回退发生了"只是日志里的一句陈述，
#:   既进不了指标，也无法回答"为什么用户拿到的是关键词结果"。
RETRIEVAL_MODES = ("keyword", "hybrid", "degraded")

#: 降级原因闭集。与 `RETRIEVAL_MODES` 一样是指标标签，取值变化
#: 等同于指标口径变化，必须显式更新。
DEGRADED_REASONS = (
    "embedding_provider_failed",
    "vector_index_unavailable",
    "embedding_model_version_mismatch",
)


@dataclass(frozen=True)
class HybridSearchResult:
    """一次检索的**结果与过程事实**（`search_hybrid` 的返回值）。

    `hits` 与 `search()` 的返回同形（`ScoredChunk` 序列，融合后的顺序）；
    其余字段回答"这次检索是怎么跑的"：模式、降级原因、排序规则版本、
    每个候选的召回来源。它们一起进入 run 存证与指标 —— 检索模式
    只有跟着结果一起被记录，事后才分得清"关键词结果"是基线还是回退。
    """

    hits: tuple[ScoredChunk, ...]
    mode: str
    degraded_reason: str
    ranking_version: str
    #: `(chunk_id, 来源标签)`，来源标签为 `keyword` / `vector` / `keyword+vector`。
    recall_sources: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in RETRIEVAL_MODES:
            raise ValueError(f"mode 必须是 {RETRIEVAL_MODES} 之一，收到 {self.mode!r}")
        if self.mode == "degraded":
            if self.degraded_reason not in DEGRADED_REASONS:
                raise ValueError(
                    f"degraded 必须携带 {DEGRADED_REASONS} 之一的原因，"
                    f"收到 {self.degraded_reason!r}"
                )
        elif self.degraded_reason:
            raise ValueError(f"只有 degraded 模式可以携带降级原因，当前 mode 是 {self.mode!r}")
        if not self.ranking_version:
            raise ValueError("ranking_version 不能为空")

    def as_decision(self) -> RetrievalDecision:
        """转成教学 run 的存证视图（模式 / 原因 / 排序规则版本）。"""
        return RetrievalDecision(
            mode=self.mode,
            reason_code=self.degraded_reason,
            ranking_version=self.ranking_version,
        )


@dataclass(frozen=True, slots=True)
class RetrievalDecision:
    """一次检索的**存证视图**（教学 run 持久化用，与 `RoutingDecision` 同构）。

    为什么不直接存 `HybridSearchResult`：那个对象携带候选与召回来源
    （随每次检索变化、体积大），而 run 存证需要的是**可聚合的稳定事实**
    —— 模式、原因、排序规则版本。字段收敛进闭集，指标渲染才能用
    白名单而不是透传任意标签。
    """

    mode: str
    reason_code: str
    ranking_version: str
    policy_version: str = "retrieval-route/v1"

    def __post_init__(self) -> None:
        if self.mode not in RETRIEVAL_MODES:
            raise ValueError(f"mode 必须是 {RETRIEVAL_MODES} 之一，收到 {self.mode!r}")
        if self.mode == "degraded":
            if self.reason_code not in DEGRADED_REASONS:
                raise ValueError(
                    f"degraded 的 reason_code 必须是 {DEGRADED_REASONS} 之一，"
                    f"收到 {self.reason_code!r}"
                )
        elif self.reason_code != "":
            raise ValueError(f"{self.mode} 模式不携带原因，收到 {self.reason_code!r}")
        if not self.ranking_version:
            raise ValueError("ranking_version 不能为空")
        if self.policy_version != "retrieval-route/v1":
            raise ValueError("不支持的检索存证策略版本")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_version": self.policy_version,
            "mode": self.mode,
            "reason_code": self.reason_code,
            "ranking_version": self.ranking_version,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RetrievalDecision:
        if not isinstance(raw, dict) or set(raw) != {
            "policy_version",
            "mode",
            "reason_code",
            "ranking_version",
        }:
            raise ValueError("检索存证字段无效")
        if not all(isinstance(value, str) for value in raw.values()):
            raise ValueError("检索存证值必须是文本")
        return cls(
            policy_version=raw["policy_version"],
            mode=raw["mode"],
            reason_code=raw["reason_code"],
            ranking_version=raw["ranking_version"],
        )


@dataclass(frozen=True)
class RetrievalAssessment:
    """一次检索的**两个独立结论**。

    刻意分成两个字段而不是一个字符串：证据状态回答"核心结论有没有足够证据"，
    过程健康度回答"这次检索有没有按预期跑完"。合并成一个的那次改造留下过
    一个具体的错误 —— 外部抓取失败时仍然返回 `supported`，
    因为"检索顺利"被当成了"结论有据"。

    `signals` 一并带上：它是判定的**机械输入**。只给结论不给输入，
    排障时就得反推判定器，而反推出来的规则往往与实际实现不同。
    """

    evidence: EvidenceAssessment
    health: RetrievalHealth
    signals: RetrievalSignals

    @property
    def state(self) -> str:
        return str(self.evidence.state)

    def to_dict(self) -> dict:
        return {
            "retrieval_health": str(self.health),
            "evidence": self.evidence.to_dict(),
        }


class KnowledgeRepository:
    """项目内检索的**唯一**入口。

    注意这里是"仓储"而不是"适配器"：它的方法都没有后端分支，
    后端差异全部由注入的 `IngestionRepository` 承担。
    """

    def __init__(
        self,
        *,
        ingestion: IngestionRepository,
        relevance_floor: int = RELEVANCE_FLOOR,
        embedding_provider: EmbeddingProvider | None = None,
        vector_index: VectorIndex | None = None,
    ) -> None:
        """向量组件**成对注入、或都不注入**。

        只配一半（例如有 provider 没有 index）意味着混合路径注定拿不到
        向量候选 —— 那不是"尽力而为"，而是装配错误。与其在每次检索时
        静默降级，不如在构造期拒绝：错误配置应该当场炸，而不是变成
        一条"所有请求都 degraded"的运行时指标。
        """
        self._ingestion = ingestion
        self._relevance_floor = relevance_floor
        if (embedding_provider is None) != (vector_index is None):
            raise ValueError(
                "embedding_provider 与 vector_index 必须成对注入或同时缺省；"
                "只配一半的混合检索注定降级，应在装配期暴露"
            )
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index

    def search(
        self,
        actor: Principal,
        project_id: str,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[ScoredChunk, ...]:
        """在**本项目**范围内检索，返回按确定性分数排序的候选。

        `actor` 是必需的：作用域过滤要按主体判定成员关系（未授予的主体
        连"这个项目有几条片段"都不该知道）。把它做成可选参数会立刻出现
        "忘了传"的调用点，而那种调用默认拿到什么，取决于实现者的心情。

        本方法是 `search_hybrid()` 的薄封装（只取 `hits`）：现有调用方
        （teaching 上下文、测试）不需要检索模式事实，签名与语义保持
        与第 4 轮基线完全一致。
        """
        return self.search_hybrid(actor, project_id, query, limit=limit).hits

    def search_hybrid(
        self,
        actor: Principal,
        project_id: str,
        query: str,
        *,
        limit: int = 10,
    ) -> HybridSearchResult:
        """混合检索：关键词基线 + 可选向量候选 + 确定性 RRF 融合。

        ## 降级规则（R9 任务 3 退出门）

        以下任一情况**不产生异常**，而是回退关键词结果并记录原因 ——
        向量路径的任何失败都不许弄丢关键词路径已经拿到的答案：

        1. provider 的 `model_revision` 与索引的 revision 不一致
           → `embedding_model_version_mismatch`（换模型后旧索引整体失效）；
        2. provider 调用抛任何异常（超时、网络、限流）→ `embedding_provider_failed`；
        3. 作用域内**没有任何**片段在索引中有向量 → `vector_index_unavailable`
           （索引整体未建；部分缺失只是该片段不进向量候选，不触发降级）。

        向量组件未注入时返回 `keyword` 模式 —— 基线是正常状态，不是降级。

        ## 作用域不变量

        向量候选与关键词候选来自**同一个** `stored_chunks` 收窄结果：
        租户 / 项目 / 最新版本过滤发生在 SQL / RLS 边界内（02 号规格 §7），
        索引只对已收窄的键排序，不存在"向量路径绕过 RLS"的第二条入口。
        """
        # 默认就是最新版本（`latest_only=True`）：这是**检索**该有的语义，
        # 历史版本由引用按 `document_id` 精确回读，不经检索。
        scoped = self._ingestion.stored_chunks(actor, project_id, latest_only=True)
        keyword_hits = rank_chunks(scoped, query, limit=limit)

        provider = self._embedding_provider
        index = self._vector_index
        if provider is None or index is None:
            return HybridSearchResult(
                hits=keyword_hits,
                mode="keyword",
                degraded_reason="",
                ranking_version=RANKING_VERSION,
                recall_sources=tuple(
                    (hit.chunk.chunk_id, "keyword") for hit in keyword_hits
                ),
            )

        def _degraded(reason: str) -> HybridSearchResult:
            return HybridSearchResult(
                hits=keyword_hits,
                mode="degraded",
                degraded_reason=reason,
                ranking_version=RANKING_VERSION,
                recall_sources=tuple(
                    (hit.chunk.chunk_id, "keyword") for hit in keyword_hits
                ),
            )

        if provider.model_revision != index.model_revision:
            return _degraded("embedding_model_version_mismatch")

        try:
            query_embedding = provider.embed_texts((query,))[0]
        except Exception:  # noqa: BLE001 - 任何 provider 失败都降级，绝不上抛
            return _degraded("embedding_provider_failed")

        # 键按 `stored_chunks` 的稳定顺序（document_id, chunk_index）派生：
        # 同一 input_hash 对应多个片段时（同内容不同 chunk），取顺序在前的。
        index_keys: list = []
        chunk_by_input_hash: dict[str, StoredChunk] = {}
        for chunk in scoped:
            key = embed_chunk_key(
                chunk_content_hash=chunk.content_hash,
                chunk_parser_version=chunk.parser_version,
                model_revision=provider.model_revision,
            )
            index_keys.append(key)
            chunk_by_input_hash.setdefault(key.input_hash, chunk)

        if not any(index.has(key) for key in index_keys):
            return _degraded("vector_index_unavailable")

        matches = index.search(tuple(index_keys), query_embedding, limit=limit)
        vector_hits = tuple(
            (chunk_by_input_hash[match.key.input_hash], position)
            for position, match in enumerate(matches, start=1)
        )
        fused = fuse_rrf(keyword_hits, vector_hits, limit=limit)
        recall_sources = tuple(
            (candidate.chunk.chunk_id, "+".join(candidate.recalled_from))
            for candidate in fused
        )
        return HybridSearchResult(
            hits=tuple(candidate.as_scored_chunk() for candidate in fused),
            mode="hybrid",
            degraded_reason="",
            ranking_version=HYBRID_RANKING_VERSION,
            recall_sources=recall_sources,
        )

    def read_span(
        self,
        actor: Principal,
        project_id: str,
        source_id: str,
        span: tuple[int, int],
        *,
        document_id: str,
        content_hash: str = "",
    ) -> StoredChunk | None:
        """按**引用里的不可变标识**精确回读原文切片。

        `document_id` 是必需参数，不是可选的"精细定位"。理由见
        `ArtifactRef` 的 docstring：同一来源的两版片段可能落在同一个跨度上，
        只按 `source_id + span` 回读会**静默返回另一版**（实测命中
        `doc_v2/'delta!'`、回读拿到 `doc_v1/'bravo!'`）—— 而引用看起来完全正常，
        没有任何东西会报警。所以精确读取直接按标识查库，**禁止挑第一条**。

        `content_hash` 与 `source_id` 是**附加的一致性校验**：调用方给了就核对，
        对不上返回 `None`（"你要的那一版这一段不存在"），而不是换一段内容给它。
        少了这两条，客户端可以把别人的 `document_id` 与自己的 `source_id`
        拼成一个"看起来自洽"的引用。

        **有权访问本项目时，粒度上的落空一律是同一个 `None`**：来源不存在、
        span 越界、这段片段属于本项目的另一份原文 —— 三者不可区分。
        区分它们等于提供存在性探针（探测者据此能数出你这个项目里有哪些来源）。

        ⚠️ **授权拒绝刻意不走这条 `None` 路**：项目不存在、不属于本租户、
        主体未获授予，由 `IngestionRepository` 抛出那**一个**拒绝码
        （`membership.get`，与 `search` / `get_job` 完全同源）。
        让 `read_span` 把拒绝吞成 `None` 会是**第二个**判定出口：
        同一个"不是你项目"的事实，一处抛码一处返回空，日后必然分叉。

        两条路在 HTTP 上确实逐字相同 —— 但那由 `error_response` 这个**唯一**
        出口保证（拒绝 → 404 + "资源不存在"），而不是靠每个读取方法各自
        把拒绝翻译成 `None`。"形状一致"要收在一处，否则它只是碰巧一致。

        ⚠️ 返回值里 `content` 与 `span` 的长度一致**只**由类型强制
        （`span_end - span_start == len(content)`）。"它真的是原文的切片"
        由写入侧核验（`assert_chunks_match_document`，见 `models.py`），
        不是这一层能保证的 —— 这里的注释曾经把两者混为一谈（R4-05）。
        """
        chunk = self._ingestion.chunk_at(
            actor, project_id, document_id=document_id, span=span
        )
        if chunk is None:
            return None
        if chunk.source_id != source_id:
            # 调用方给了一对不自洽的（source_id, document_id）：这不是"落空"，
            # 而是"你拼出来的引用本身矛盾"。两者对外同形（都是没找到），
            # 但这里绝不能返回那个片段 —— 否则引用会指向另一个来源。
            return None
        if content_hash and chunk.content_hash != content_hash:
            return None
        return chunk

    def assess(self, hits: tuple[ScoredChunk, ...]) -> RetrievalAssessment:
        """把候选转成证据判定。

        ⚠️ **命中不等于支持。** 本轮没有冻结的核心结论标注集，因此
        `required_claim_refs` 拿不到 → `assess_retrieval` 必然返回
        `insufficient` + `MISSING_SUPPORT`，同时过程健康度如实报告 `clean`。

        这是**正确**的结果，不是待修的缺陷：我们确实无法证明核心结论有充分证据
        （02 号规格 §4）。"有候选就报 supported"才是要避免的那个错误 ——
        错误的 `insufficient` 会被用户追问后修正，错误的 `supported` 会被直接采信。
        """
        signals = RetrievalSignals(
            # 候选数用**命中数**而不是"项目里有多少片段"：后者与本次查询无关，
            # 拿它当分子会让 NO_CANDIDATES 永不触发。
            candidate_count=len(hits),
            top_score=float(hits[0].score) if hits else 0.0,
            relevance_floor=float(self._relevance_floor),
            # 本轮没有需要跑的外部取数步骤，所以"必需步骤"必然完成。
            required_steps_completed=True,
        )
        return RetrievalAssessment(
            evidence=assess_retrieval(signals),
            health=assess_retrieval_health(signals),
            signals=signals,
        )
