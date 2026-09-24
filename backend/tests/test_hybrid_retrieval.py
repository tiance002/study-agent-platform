"""R9 任务 3：混合检索增量 —— embedding 端口、RRF 融合与降级路径。

守四件**分开**的事：

1. **版本化索引键**：scope / content hash / parser version / model
   revision 共同决定 input hash（缓存身份）；任何一项变化都必须产生
   新键，旧向量"对不上号"而不是"碰巧还被读"。scope 把租户/项目烙进
   键里 —— 跨项目同内容不共用缓存。
2. **确定性融合**：同两个候选列表必须得到逐位相同的融合结果
   （精确有理数分数 + 稳定 tie-break）。
3. **降级不丢答案**：provider 失败、索引缺失或故障、版本不匹配时
   回退关键词结果并携带闭集原因 —— 向量路径的任何失败都不许让
   关键词路径已经拿到的候选消失。
4. **同文不同片段**：同内容的多个片段共享向量键，向量命中按稳定
   顺序展开为连续排名，每个片段都拿得到向量候选。

keyword/v1 基线的回归由 `test_knowledge_search.py` 的冻结夹具守着；
本文件只测**新增**的混合路径，未注入向量组件时 `search_hybrid`
与第 4 轮基线的等价性也在这里断言一次。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from app.core.clock import SystemClock
from app.identity.membership import MembershipStore
from app.identity.models import Principal
from app.identity.ports import SystemContext
from app.knowledge.embedding import (
    HASHING_EMBEDDING_REVISION,
    EmbeddingIndexKey,
    HashingEmbeddingProvider,
    InMemoryVectorIndex,
    embed_chunk_key,
    embedding_index_key,
    embedding_scope,
)
from app.knowledge.fusion import (
    HYBRID_RANKING_VERSION,
    RRF_K,
    fuse_rrf,
)
from app.knowledge.memory_store import InMemoryIngestionRepository
from app.knowledge.models import StoredChunk
from app.knowledge.retrieval import RANKING_VERSION, ScoredChunk, rank_chunks
from app.knowledge.store import DEGRADED_REASONS, HybridSearchResult, KnowledgeRepository
from app.product.memory_store import InMemoryProductRepository

EVENT_TIME = datetime(2026, 9, 24, tzinfo=timezone.utc)
TENANT = "t_hybrid"
ACTOR_ID = "u_hybrid_alice"
SCOPE = embedding_scope(TENANT, "p_hybrid")


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _actor() -> Principal:
    return Principal(principal_id=ACTOR_ID, tenant_id=TENANT)


def _chunk(
    source_id: str, index: int, content: str, *, heading: tuple[str, ...] = ()
) -> StoredChunk:
    return StoredChunk(
        chunk_id=_unique("chk"),
        tenant_id=TENANT,
        project_id="p_hybrid",
        source_id=source_id,
        document_id=f"doc_{source_id}",
        chunk_index=index,
        heading_path=heading,
        span_start=0,
        span_end=len(content),
        content=content,
        created_at=EVENT_TIME,
    )


def _scored(chunk: StoredChunk, score: int = 1) -> ScoredChunk:
    return ScoredChunk(chunk=chunk, score=score, matched_terms=())


# ------------------------------------------------- 一、版本化索引键


@pytest.mark.invariant
def test_index_key_binds_all_identity_parts():
    """组成部分里**任何一项**变化都必须产生新的 `input_hash`。

    少绑一项，那一项的变化就会复用旧向量：换切块器后按旧切分检索、
    换 embedding 模型后在两个语义空间之间比相似度 —— 两者都没有报错。
    """
    base = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v1",
        scope=SCOPE,
    )
    same = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v1",
        scope=SCOPE,
    )
    assert base.input_hash == same.input_hash
    changed_content = embedding_index_key(
        content_hash="sha256:" + "b" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v1",
        scope=SCOPE,
    )
    changed_parser = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v2",
        model_revision="hashing-embedding/v1",
        scope=SCOPE,
    )
    changed_model = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v2",
        scope=SCOPE,
    )
    changed_scope = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v1",
        scope=embedding_scope(TENANT, "p_other"),
    )
    assert len(
        {
            base.input_hash,
            changed_content.input_hash,
            changed_parser.input_hash,
            changed_model.input_hash,
            changed_scope.input_hash,
        }
    ) == 5


@pytest.mark.invariant
def test_index_key_scope_isolates_projects_and_tenants():
    """跨项目 / 跨租户的同内容片段必须派生**不同**的缓存身份。

    计划不变量：跨项目不能命中、读回或复用缓存。向量本身是内容的
    确定性函数（复用不会算错），但键上的隔离杜绝一个项目的索引存量
    影响另一个项目的缓存身份。
    """
    same_tenant_other_project = embedding_scope(TENANT, "p_other")
    other_tenant = embedding_scope("t_other", "p_hybrid")
    assert same_tenant_other_project != SCOPE
    assert other_tenant != SCOPE
    parts = dict(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v1",
    )
    key_here = embedding_index_key(scope=SCOPE, **parts)
    key_other_project = embedding_index_key(scope=same_tenant_other_project, **parts)
    key_other_tenant = embedding_index_key(scope=other_tenant, **parts)
    assert len({key_here.input_hash, key_other_project.input_hash, key_other_tenant.input_hash}) == 3
    # 空 scope / 空标识在构造期拒绝。
    with pytest.raises(ValueError):
        embedding_scope("", "p_hybrid")
    with pytest.raises(ValueError):
        embedding_scope(TENANT, "")


@pytest.mark.invariant
def test_index_key_rejects_inconsistent_input_hash():
    """`input_hash` 是派生指纹，与组成部分不一致的键在构造期被拒绝。"""
    with pytest.raises(ValueError, match="input_hash"):
        EmbeddingIndexKey(
            content_hash="sha256:" + "a" * 64,
            parser_version="structure/v1",
            model_revision="hashing-embedding/v1",
            scope=SCOPE,
            input_hash="sha256:" + "0" * 64,
        )


# ------------------------------------------------- 二、确定性 provider 与索引


@pytest.mark.invariant
def test_hashing_provider_is_deterministic_and_dimension_stable():
    provider = HashingEmbeddingProvider()
    first = provider.embed_texts(("主从复制", "索引讲义"))
    second = provider.embed_texts(("主从复制", "索引讲义"))
    assert first == second, "同 revision 下同文本必须产生同一向量"
    assert len(first) == 2
    assert all(len(vector) == len(first[0]) for vector in first)
    # 不同文本的向量互不相同：64 维下的哈希碰撞实际上不可能。
    assert first[0] != first[1]


@pytest.mark.invariant
def test_in_memory_index_rejects_foreign_revision_and_upserts_idempotently():
    index = InMemoryVectorIndex(model_revision=HASHING_EMBEDDING_REVISION)
    provider = HashingEmbeddingProvider()
    key = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision=HASHING_EMBEDDING_REVISION,
        scope=SCOPE,
    )
    vector = provider.embed_texts(("文本",))[0]
    index.upsert(key, vector)
    index.upsert(key, vector)  # 幂等覆盖，不报错
    assert index.has(key)

    foreign = embedding_index_key(
        content_hash="sha256:" + "a" * 64,
        parser_version="structure/v1",
        model_revision="hashing-embedding/v2",
        scope=SCOPE,
    )
    with pytest.raises(ValueError, match="revision"):
        index.upsert(foreign, vector)


@pytest.mark.invariant
def test_index_search_ranks_exact_match_first_and_skips_unindexed_keys():
    """查询向量与某条目完全相同时相似度为 1（自身余弦）；未索引的键被跳过。"""
    provider = HashingEmbeddingProvider()
    index = InMemoryVectorIndex(model_revision=HASHING_EMBEDDING_REVISION)
    target_text = "主库负责写入，从库异步接收变更"
    other_text = "索引让查询不必扫描整张表"
    for text in (target_text, other_text):
        index.upsert(
            embedding_index_key(
                content_hash="sha256:" + text[:64].ljust(64, "0"),
                parser_version="structure/v1",
                model_revision=HASHING_EMBEDDING_REVISION,
                scope=SCOPE,
            ),
            provider.embed_texts((text,))[0],
        )
    query = provider.embed_texts((target_text,))[0]
    keys = tuple(
        embedding_index_key(
            content_hash="sha256:" + text[:64].ljust(64, "0"),
            parser_version="structure/v1",
            model_revision=HASHING_EMBEDDING_REVISION,
            scope=SCOPE,
        )
        for text in (target_text, other_text)
    ) + (
        # 在键集合里但从未 upsert：部分缺向量是常态，跳过而不是报错。
        embedding_index_key(
            content_hash="sha256:" + "c" * 64,
            parser_version="structure/v1",
            model_revision=HASHING_EMBEDDING_REVISION,
            scope=SCOPE,
        ),
    )
    matches = index.search(keys, query, limit=5)
    assert len(matches) == 2
    assert matches[0].similarity == pytest.approx(1.0)
    assert matches[0].key.content_hash != matches[1].key.content_hash


# ------------------------------------------------- 三、确定性 RRF 融合


@pytest.mark.invariant
def test_rrf_prefers_candidates_recalled_by_both_lists():
    """双路命中的候选融合分严格大于任一单路的第一名。

    1/(k+1) + 1/(k+2) > 1/(k+1)：这正是选 RRF 的理由 ——
    没有"归一化后加权"的校准问题，双路共识仍然自然胜出。
    """
    both = _chunk("src_both", 0, "双路命中")
    keyword_only = _chunk("src_kw", 0, "只有关键词")
    vector_only = _chunk("src_vec", 0, "只有向量")
    keyword_hits = (_scored(both, 90), _scored(keyword_only, 50))
    vector_hits = ((vector_only, 1), (both, 2))
    fused = fuse_rrf(keyword_hits, vector_hits, limit=3)
    assert fused[0].chunk.source_id == "src_both"
    assert fused[0].recalled_from == ("keyword", "vector")
    from fractions import Fraction

    assert fused[0].rrf_score == Fraction(1, RRF_K + 1) + Fraction(1, RRF_K + 2)
    sources = {candidate.chunk.source_id: candidate.recalled_from for candidate in fused}
    assert sources["src_kw"] == ("keyword",)
    assert sources["src_vec"] == ("vector",)


@pytest.mark.invariant
def test_rrf_is_deterministic_and_tiebreaks_stably():
    """同输入同输出；同分候选按 `(source_id, chunk_index)` 稳定排序。"""
    a = _chunk("src_a", 1, "内容甲")
    b = _chunk("src_a", 0, "内容乙")
    keyword_hits = (_scored(a, 10),)
    vector_hits = ((b, 1), (a, 1))
    first = fuse_rrf(keyword_hits, vector_hits, limit=5)
    second = fuse_rrf(keyword_hits, vector_hits, limit=5)
    assert [c.chunk.chunk_id for c in first] == [c.chunk.chunk_id for c in second]
    # a：keyword rank 1 + vector rank 1；b：仅 vector rank 1 —— a 必须在前。
    assert first[0].chunk.chunk_id == a.chunk_id


@pytest.mark.invariant
def test_fused_candidate_preserves_keyword_evidence():
    """融合决定顺序，关键词侧的分数与命中项必须原样保留。"""
    chunk = _chunk("src_a", 0, "回滚撤销修改")
    evidence = ScoredChunk(chunk=chunk, score=113, matched_terms=("回滚",))
    fused = fuse_rrf((evidence,), (), limit=5)
    assert fused[0].as_scored_chunk().score == 113
    assert fused[0].as_scored_chunk().matched_terms == ("回滚",)
    # 纯向量候选没有关键词证据：score=0，但片段完整。
    vector_only = fuse_rrf((), ((chunk, 1),), limit=5)
    assert vector_only[0].as_scored_chunk().score == 0
    assert vector_only[0].as_scored_chunk().chunk.chunk_id == chunk.chunk_id


# ------------------------------------------------- 四、HybridSearchResult 契约


@pytest.mark.invariant
def test_hybrid_result_enforces_closed_labels():
    good_kw = HybridSearchResult(
        hits=(), mode="keyword", degraded_reason="", ranking_version=RANKING_VERSION
    )
    assert good_kw.mode == "keyword"
    with pytest.raises(ValueError, match="mode"):
        HybridSearchResult(hits=(), mode="semantic", degraded_reason="", ranking_version="v")
    with pytest.raises(ValueError, match="degraded"):
        HybridSearchResult(
            hits=(), mode="degraded", degraded_reason="not_in_set", ranking_version="v"
        )
    with pytest.raises(ValueError, match="降级原因"):
        HybridSearchResult(
            hits=(), mode="keyword", degraded_reason="embedding_provider_failed", ranking_version="v"
        )


# ------------------------------------------------- 五、混合检索路径（真实仓储）


@pytest.fixture()
def repo() -> KnowledgeRepository:
    """只搭内存仓储（混合逻辑与后端无关；隔离已由检索边界测试守着）。"""
    membership = MembershipStore()
    products = InMemoryProductRepository(membership=membership)
    ingestion = InMemoryIngestionRepository(
        membership=membership, products=products, clock=SystemClock()
    )
    return KnowledgeRepository(ingestion=ingestion)


def _ingest(repo: KnowledgeRepository, actor: Principal, project_id: str, content: str) -> None:
    """走真实管线入料（与 `test_knowledge_search._ingest` 同一路径）。"""
    from app.knowledge.processor import DocumentProcessor

    ingestion = repo._ingestion  # noqa: SLF001 - 测试直达注入的仓储
    source_id = _unique("src")
    ingestion.products.register_source(
        actor,
        project_id,
        source_id=source_id,
        display_name="混合检索资料",
        media_type="text/markdown",
        identity_hash="sha256:" + source_id,
        acquisition={"kind": "upload"},
    )
    document, queued = ingestion.enqueue(
        actor,
        project_id,
        source_id,
        document_id=_unique("doc"),
        job_id=_unique("job"),
        title="混合检索资料",
        content=content,
        media_type="text/markdown",
        language="zh",
    )
    for _ in range(50):
        job = ingestion.claim_next(worker_id="hybrid-test", lease_seconds=60)
        if job is None:
            break
        if job.job_id == queued.job_id:
            ingestion.complete(job, DocumentProcessor().parse(document))
            return
        ingestion.complete(job, DocumentProcessor().parse(ingestion.load_document(job)))
    raise AssertionError("没能认领到目标任务")


def _project_id(repo: KnowledgeRepository) -> str:
    ingestion = repo._ingestion  # noqa: SLF001 - 测试直达注入的仓储
    context = SystemContext(TENANT, "混合检索测试")
    project_id = _unique("proj")
    ingestion.membership.create_project(context, project_id=project_id, name="混合检索")
    ingestion.membership.grant_project(context, principal_id=ACTOR_ID, project_id=project_id)
    return project_id


def _build_index(
    repo: KnowledgeRepository,
    actor: Principal,
    project_id: str,
) -> InMemoryVectorIndex:
    """用与生产路径完全相同的键规则为已收窄片段建向量索引。"""
    provider = HashingEmbeddingProvider()
    index = InMemoryVectorIndex(model_revision=provider.model_revision)
    scope = embedding_scope(actor.tenant_id, project_id)
    for chunk in repo._ingestion.stored_chunks(actor, project_id, latest_only=True):  # noqa: SLF001
        key = embed_chunk_key(
            chunk_content_hash=chunk.content_hash,
            chunk_parser_version=chunk.parser_version,
            model_revision=provider.model_revision,
            scope=scope,
        )
        index.upsert(key, provider.embed_texts((chunk.content,))[0])
    return index


DOCUMENT = (
    "# 事务\n\n事务是一组要么全部成功、要么全部失败的操作。\n\n"
    "## 回滚\n\n回滚撤销本事务已经执行的全部修改。\n\n"
    "## 提交\n\n提交把修改持久化到磁盘。\n"
)


def test_search_hybrid_without_components_matches_keyword_baseline(repo: KnowledgeRepository):
    """未注入向量组件 → `keyword` 模式，结果与第 4 轮基线逐条一致。"""
    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)

    result = repo.search_hybrid(actor, project_id, "回滚", limit=5)
    baseline = rank_chunks(
        repo._ingestion.stored_chunks(actor, project_id, latest_only=True),  # noqa: SLF001
        "回滚",
        limit=5,
    )
    assert result.mode == "keyword"
    assert result.degraded_reason == ""
    assert result.ranking_version == RANKING_VERSION
    assert result.hits == baseline
    assert repo.search(actor, project_id, "回滚", limit=5) == baseline


def test_search_hybrid_blends_keyword_and_vector_candidates(repo: KnowledgeRepository):
    """注入组件且索引已建 → `hybrid` 模式，RRF 融合顺序生效。"""
    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)
    provider = HashingEmbeddingProvider()
    index = _build_index(repo, actor, project_id)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=provider,
        vector_index=index,
    )

    result = hybrid_repo.search_hybrid(actor, project_id, "回滚撤销本事务已经执行的全部修改", limit=5)
    assert result.mode == "hybrid"
    assert result.ranking_version == HYBRID_RANKING_VERSION
    assert result.hits, "混合模式必须返回候选"
    sources = dict(result.recall_sources)
    top_chunk_id = result.hits[0].chunk.chunk_id
    assert sources[top_chunk_id] == "keyword+vector"
    # 同一查询两次执行结果逐位一致（确定性）。
    again = hybrid_repo.search_hybrid(actor, project_id, "回滚撤销本事务已经执行的全部修改", limit=5)
    assert [h.chunk.chunk_id for h in again.hits] == [h.chunk.chunk_id for h in result.hits]


def test_search_hybrid_degrades_on_provider_failure_without_losing_answers(repo: KnowledgeRepository):
    """provider 抛任何异常 → degraded + `embedding_provider_failed`，关键词结果原样保留。"""

    class _ExplodingProvider(HashingEmbeddingProvider):
        def embed_texts(self, texts):
            raise TimeoutError("provider 超时")

    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)
    index = _build_index(repo, actor, project_id)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=_ExplodingProvider(),
        vector_index=index,
    )

    result = hybrid_repo.search_hybrid(actor, project_id, "回滚", limit=5)
    baseline = repo.search_hybrid(actor, project_id, "回滚", limit=5)
    assert result.mode == "degraded"
    assert result.degraded_reason == "embedding_provider_failed"
    assert result.ranking_version == RANKING_VERSION
    assert result.hits == baseline.hits, "降级绝不允许弄丢关键词路径已经拿到的候选"


def test_search_hybrid_degrades_on_model_version_mismatch(repo: KnowledgeRepository):
    """索引 revision 与 provider revision 不一致 → 整体降级，不读旧空间的向量。"""
    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)
    index = _build_index(repo, actor, project_id)
    # 索引按旧 revision 建；provider 声明新 revision。
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=HashingEmbeddingProvider(),
        vector_index=InMemoryVectorIndex(model_revision="hashing-embedding/v2"),
    )
    assert hybrid_repo.search_hybrid(actor, project_id, "回滚", limit=5).degraded_reason == (
        "embedding_model_version_mismatch"
    )
    # 反向：provider 旧、索引新 —— 同样是不匹配，同样降级。
    hybrid_repo_new_index = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=HashingEmbeddingProvider(),
        vector_index=index,
    )
    hybrid_repo_new_index._vector_index = InMemoryVectorIndex(model_revision="other/v9")  # noqa: SLF001
    result = hybrid_repo_new_index.search_hybrid(actor, project_id, "回滚", limit=5)
    assert result.mode == "degraded"
    assert result.degraded_reason == "embedding_model_version_mismatch"


def test_search_hybrid_degrades_when_index_has_no_vectors(repo: KnowledgeRepository):
    """作用域内没有任何已索引片段 → `vector_index_unavailable`；部分缺失不降级。"""
    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)

    empty_index = InMemoryVectorIndex(model_revision=HASHING_EMBEDDING_REVISION)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=HashingEmbeddingProvider(),
        vector_index=empty_index,
    )
    result = hybrid_repo.search_hybrid(actor, project_id, "回滚", limit=5)
    assert result.mode == "degraded"
    assert result.degraded_reason == "vector_index_unavailable"
    assert result.hits, "降级后关键词结果仍然在场"

    # 部分缺失：只为第一个片段建向量 —— 不触发整体降级，仍走 hybrid。
    provider = HashingEmbeddingProvider()
    partial = InMemoryVectorIndex(model_revision=provider.model_revision)
    scoped = repo._ingestion.stored_chunks(actor, project_id, latest_only=True)  # noqa: SLF001
    first = scoped[0]
    partial.upsert(
        embed_chunk_key(
            chunk_content_hash=first.content_hash,
            chunk_parser_version=first.parser_version,
            model_revision=provider.model_revision,
            scope=embedding_scope(actor.tenant_id, project_id),
        ),
        provider.embed_texts((first.content,))[0],
    )
    partial_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=provider,
        vector_index=partial,
    )
    partial_result = partial_repo.search_hybrid(actor, project_id, "事务", limit=5)
    assert partial_result.mode == "hybrid"


def test_partial_configuration_is_rejected_at_construction(repo: KnowledgeRepository):
    """只配 provider 或只配 index 是装配错误，构造期拒绝。"""
    with pytest.raises(ValueError, match="成对"):
        KnowledgeRepository(
            ingestion=repo._ingestion,  # noqa: SLF001
            embedding_provider=HashingEmbeddingProvider(),
        )
    with pytest.raises(ValueError, match="成对"):
        KnowledgeRepository(
            ingestion=repo._ingestion,  # noqa: SLF001
            vector_index=InMemoryVectorIndex(model_revision=HASHING_EMBEDDING_REVISION),
        )


def test_degraded_reasons_match_closed_set():
    """`DEGRADED_REASONS` 就是降级原因的全集：新增原因 = 改指标口径，必须显式。"""
    assert set(DEGRADED_REASONS) == {
        "embedding_provider_failed",
        "vector_index_unavailable",
        "embedding_model_version_mismatch",
    }


def test_search_hybrid_degrades_on_index_failure(repo: KnowledgeRepository):
    """索引 `has()` / `search()` 抛异常 → `vector_index_unavailable`，关键词答案不丢。

    索引故障（存储不可用、损坏、超时）与"索引未建"对调用方是同一件事：
    向量路径不可用，回退关键词。异常上抛会让一次向量索引故障
    弄丢整条检索路径 —— 这正是降级闭集要挡住的场景。
    """

    class _ExplodingHasIndex(InMemoryVectorIndex):
        def has(self, key):
            raise RuntimeError("index storage unavailable")

    class _ExplodingSearchIndex(InMemoryVectorIndex):
        def search(self, keys, embedding, *, limit):
            raise TimeoutError("index search timeout")

    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)
    baseline = repo.search_hybrid(actor, project_id, "回滚", limit=5)

    for broken in (_ExplodingHasIndex, _ExplodingSearchIndex):
        # 先注入正常索引保证降级发生在对应环节（有向量可查）。
        working = _build_index(repo, actor, project_id)
        broken_index = broken(model_revision=working.model_revision)
        if broken is _ExplodingSearchIndex:
            broken_index._vectors = dict(working._vectors)  # noqa: SLF001 - 故障注入前置状态
        hybrid_repo = KnowledgeRepository(
            ingestion=repo._ingestion,  # noqa: SLF001
            embedding_provider=HashingEmbeddingProvider(),
            vector_index=broken_index,
        )
        result = hybrid_repo.search_hybrid(actor, project_id, "回滚", limit=5)
        assert result.mode == "degraded", broken.__name__
        assert result.degraded_reason == "vector_index_unavailable", broken.__name__
        assert result.hits == baseline.hits, "索引故障同样不允许弄丢关键词候选"


def test_search_hybrid_gives_every_same_content_chunk_a_vector_rank(repo: KnowledgeRepository):
    """同内容不同片段共享向量键：向量命中必须**展开**到每个片段。

    只保留顺序在前的片段会让其余同文片段永远拿不到向量候选，
    RRF 得分被 `stored_chunks` 的存储顺序单方面决定。
    """
    actor = _actor()
    project_id = _project_id(repo)
    # 两个来源、同一段内容 → 两个片段、同一个 content_hash / input_hash。
    _ingest(repo, actor, project_id, "回滚撤销本事务已经执行的全部修改")
    _ingest(repo, actor, project_id, "回滚撤销本事务已经执行的全部修改")
    scoped = repo._ingestion.stored_chunks(actor, project_id, latest_only=True)  # noqa: SLF001
    hashes = {chunk.content_hash for chunk in scoped}
    assert len(hashes) == 1, "测试前提：两个片段内容相同、共享 content_hash"
    assert len(scoped) == 2

    provider = HashingEmbeddingProvider()
    index = _build_index(repo, actor, project_id)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=provider,
        vector_index=index,
    )
    result = hybrid_repo.search_hybrid(actor, project_id, "回滚撤销本事务已经执行的全部修改", limit=5)
    assert result.mode == "hybrid"
    # 每个片段都必须从向量路召回（与关键词共同形成 keyword+vector）。
    sources = dict(result.recall_sources)
    for chunk in scoped:
        assert sources.get(chunk.chunk_id) == "keyword+vector", (
            "同文片段必须拿到向量候选，而不是只有存储顺序在前的那个"
        )


def test_search_hybrid_does_not_reuse_cross_project_vectors(repo: KnowledgeRepository):
    """跨项目同内容：A 项目的索引存量对 B 项目等于不存在（键上有 scope）。

    B 项目未建索引时必须整体降级，而不是"碰巧命中"A 项目写入的
    同内容向量 —— 计划不变量：跨项目不能命中、读回或复用缓存。
    """
    actor = _actor()
    project_a = _project_id(repo)
    project_b = _project_id(repo)
    _ingest(repo, actor, project_a, "回滚撤销本事务已经执行的全部修改")
    _ingest(repo, actor, project_b, "回滚撤销本事务已经执行的全部修改")

    # 只为 A 项目建索引（键含 A 的 scope）。
    index = _build_index(repo, actor, project_a)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=HashingEmbeddingProvider(),
        vector_index=index,
    )
    result_a = hybrid_repo.search_hybrid(actor, project_a, "回滚", limit=5)
    assert result_a.mode == "hybrid", "A 项目自己建了索引，必须正常混合"

    result_b = hybrid_repo.search_hybrid(actor, project_b, "回滚", limit=5)
    assert result_b.mode == "degraded"
    assert result_b.degraded_reason == "vector_index_unavailable"
    assert result_b.hits, "B 项目降级后关键词候选仍然在场"


def test_build_context_freezes_actual_retrieval_ranking_version(repo: KnowledgeRepository):
    """教学快照冻结**本次检索实际使用**的排序版本，而不是 run 行的预期。

    run 行建行时写的是 keyword/v1（当时的预期）；本次检索如果走了
    混合路径，快照必须是 hybrid-rrf/v1 —— 重放与归因都以实际版本为准。
    """
    from app.teaching.context import build_context
    from app.teaching.runs import RunStatus, TeachingRun

    actor = _actor()
    project_id = _project_id(repo)
    _ingest(repo, actor, project_id, DOCUMENT)
    hybrid_repo = KnowledgeRepository(
        ingestion=repo._ingestion,  # noqa: SLF001
        embedding_provider=HashingEmbeddingProvider(),
        vector_index=_build_index(repo, actor, project_id),
    )
    run = TeachingRun(
        run_id=_unique("run"),
        tenant_id=TENANT,
        project_id=project_id,
        conversation_id=_unique("conv"),
        user_message_id=_unique("msg"),
        principal_id=ACTOR_ID,
        answer_message_id=None,
        question="回滚撤销本事务已经执行的全部修改",
        status=RunStatus.QUEUED,
        attempt_count=0,
        model_id="scripted",
        prompt_version="teaching/v1",
        ranking_version=RANKING_VERSION,  # 建行时的预期：keyword/v1
        grounding=None,
        error_code="",
        error_detail="",
        created_at=EVENT_TIME,
        updated_at=EVENT_TIME,
    )

    context = build_context(run, actor, project_id, knowledge=hybrid_repo, history=())
    assert context.retrieval.mode == "hybrid"
    assert context.retrieval.ranking_version == HYBRID_RANKING_VERSION
    assert context.snapshot.ranking_version == HYBRID_RANKING_VERSION, (
        "快照必须记录实际检索版本，而不是 run 行建行时的预期"
    )
