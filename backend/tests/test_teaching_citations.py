"""引用校验的定向测试：伪造 / 跨项目 / hash 不符 / 回读失败 / grounding。

计划任务 3 的核心断言：**校验通过只证明"指向本次已授权快照的那一版"**，
不证明语义支持；证据状态因此维持保守。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from app.core.artifacts import DisplayPolicy
from app.identity.membership import MembershipStore
from app.identity.models import Principal
from app.knowledge.memory_store import InMemoryIngestionRepository
from app.knowledge.models import CHUNK_PARSER_VERSION, StoredChunk
from app.knowledge.store import KnowledgeRepository
from app.product.memory_store import InMemoryProductRepository
from app.teaching.context import RetrievalSnapshot, build_snapshot
from app.teaching.models import MaterialSnippet, RawCitation
from app.teaching.validation import (
    REJECT_HASH_MISMATCH,
    REJECT_NOT_IN_SNAPSHOT,
    REJECT_READBACK_FAILED,
    Grounding,
    validate_citations,
)

CONTENT = "# 事务\n\nACID 是事务的四个性质。\n\n原子性指全部成功或全部失败。\n"


@pytest.fixture()
def env():
    membership = MembershipStore()
    products = InMemoryProductRepository(membership=membership)
    ingestion = InMemoryIngestionRepository(membership=membership, products=products)
    return membership, products, ingestion, KnowledgeRepository(ingestion=ingestion)


def _actor() -> Principal:
    return Principal(
        principal_id=f"u_{uuid.uuid4().hex[:8]}", tenant_id=f"t_{uuid.uuid4().hex[:8]}"
    )


def _seed(env, actor: Principal, *, content: str = CONTENT):
    membership, products, ingestion, knowledge = env
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    membership.create_project_for(actor, project_id=project_id, name="教学", goal="")
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    products.register_source(
        actor,
        project_id,
        source_id=source_id,
        display_name="讲义.md",
        media_type="text/markdown",
        identity_hash=f"sha256:{uuid.uuid4().hex}",
        acquisition={"kind": "upload"},
    )
    document, job = ingestion.enqueue(
        actor,
        project_id,
        source_id,
        document_id=f"doc_{uuid.uuid4().hex[:8]}",
        job_id=f"job_{uuid.uuid4().hex[:8]}",
        title="讲义",
        content=content,
        media_type="text/markdown",
        language="zh",
    )
    claimed = ingestion.claim_next(worker_id="test-worker", lease_seconds=300)
    assert claimed is not None and claimed.job_id == job.job_id
    chunk = StoredChunk(
        chunk_id=f"chk_{uuid.uuid4().hex[:8]}",
        tenant_id=document.tenant_id,
        project_id=document.project_id,
        source_id=document.source_id,
        document_id=document.document_id,
        chunk_index=0,
        heading_path=(),
        heading_level=0,
        span_start=0,
        span_end=len(content),
        content=content,
        parser_version=CHUNK_PARSER_VERSION,
        display_policy=DisplayPolicy.FULL,
        created_at=datetime.now(timezone.utc),
    )
    ingestion.complete(claimed, (chunk,))
    return actor, project_id, document, chunk


def _snapshot_for(env, actor: Principal, project_id: str, question: str) -> RetrievalSnapshot:
    knowledge = env[3]
    hits = knowledge.search(actor, project_id, question, limit=5)
    return build_snapshot(
        actor, project_id, knowledge=knowledge, hits=hits, ranking_version="keyword/v1"
    )


def _raw_from_item(item: MaterialSnippet) -> RawCitation:
    return RawCitation(
        source_id=item.source_id,
        document_id=item.document_id,
        span_start=item.span_start,
        span_end=item.span_end,
        content_hash=item.content_hash,
    )


def test_exact_snapshot_citation_is_accepted(env):
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    assert snapshot.items, "前提：检索确实命中"
    validation = validate_citations(
        actor, project_id, (_raw_from_item(snapshot.items[0]),),
        snapshot=snapshot, knowledge=env[3],
    )
    assert len(validation.accepted) == 1
    assert validation.grounding is Grounding.SOURCED
    accepted = validation.accepted[0]
    assert accepted["document_id"] == snapshot.items[0].document_id


def test_forged_citation_outside_snapshot_is_rejected(env):
    """编造的 source_id / 别的项目的资料：不在快照里，直接拒绝。"""
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    forged = RawCitation(
        source_id="src_forged",
        document_id="doc_forged",
        span_start=0,
        span_end=5,
        content_hash="sha256:deadbeefdeadbeef",
    )
    validation = validate_citations(
        actor, project_id, (forged,), snapshot=snapshot, knowledge=env[3]
    )
    assert validation.accepted == ()
    assert validation.rejected[0][1] == REJECT_NOT_IN_SNAPSHOT
    assert validation.grounding is Grounding.INFERENCE_ONLY


def test_hash_mismatch_is_named_precisely(env):
    """同源同跨度但 hash 被改：拒绝原因必须是 hash，而不是笼统的"不在快照"。"""
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    item = snapshot.items[0]
    tampered = RawCitation(
        source_id=item.source_id,
        document_id=item.document_id,
        span_start=item.span_start,
        span_end=item.span_end,
        content_hash="sha256:" + "0" * 16,
    )
    validation = validate_citations(
        actor, project_id, (tampered,), snapshot=snapshot, knowledge=env[3]
    )
    assert validation.accepted == ()
    assert validation.rejected[0][1] == REJECT_HASH_MISMATCH


def test_readback_mismatch_is_caught_even_if_keys_match(env):
    """键与快照一致、但存储内容对不上（快照后原文被换版）：回读关卡兜底。"""
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    item = snapshot.items[0]
    # 构造一个"键相同、内容不同"的快照项（模拟快照与存储漂移）。
    drifted = MaterialSnippet(
        source_id=item.source_id,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        span_start=item.span_start,
        span_end=item.span_end,
        content="被换过的内容",
        content_hash=item.content_hash,
        parser_version=item.parser_version,
    )
    drifted_snapshot = RetrievalSnapshot(
        ranking_version="keyword/v1", items=(drifted,)
    )
    validation = validate_citations(
        actor,
        project_id,
        (_raw_from_item(drifted),),
        snapshot=drifted_snapshot,
        knowledge=env[3],
    )
    assert validation.accepted == ()
    assert validation.rejected[0][1] == REJECT_READBACK_FAILED


def test_duplicate_citations_are_deduplicated(env):
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    raw = _raw_from_item(snapshot.items[0])
    validation = validate_citations(
        actor, project_id, (raw, raw), snapshot=snapshot, knowledge=env[3]
    )
    assert len(validation.accepted) == 1


def test_no_valid_citations_means_inference_only(env):
    """全部被拒：回答以 inference_only 落定 —— 不伪造"有据"的状态。"""
    actor = _actor()
    _, project_id, _, _ = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    forged = RawCitation("src_x", "doc_x", 0, 1, "sha256:x")
    validation = validate_citations(
        actor, project_id, (forged,), snapshot=snapshot, knowledge=env[3]
    )
    assert validation.grounding is Grounding.INFERENCE_ONLY


def test_citation_of_another_tenant_never_reads_back(env):
    """跨租户：就算把别人的坐标抄进引用，read_span 也取不到内容。"""
    actor = _actor()
    _, project_id, document, chunk = _seed(env, actor)
    snapshot = _snapshot_for(env, actor, project_id, "ACID")
    other = _actor()
    # 别的租户 + 自己的项目（归属判定会先拒绝）—— 引用的五元组来自快照，
    # 但校验以调用方 actor 执行回读，越权坐标走不到内容。
    validation = validate_citations(
        other, project_id, (_raw_from_item(snapshot.items[0]),),
        snapshot=snapshot, knowledge=env[3],
    )
    assert validation.accepted == ()
