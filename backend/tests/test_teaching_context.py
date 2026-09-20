"""教学上下文构建的定向测试：快照冻结、外发白名单、历史预算、问题上限。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from app.core.artifacts import DisplayPolicy
from app.core.errors import ErrorCode, PlatformError
from app.identity.membership import MembershipStore
from app.identity.models import Principal
from app.knowledge.memory_store import InMemoryIngestionRepository
from app.knowledge.models import (
    CHUNK_PARSER_VERSION,
    StoredChunk,
)
from app.knowledge.store import KnowledgeRepository
from app.product.memory_store import InMemoryProductRepository
from app.product.models import MessageRole
from app.teaching.context import (
    DEFAULT_MAX_MATERIALS,
    MAX_QUESTION_CHARS,
    build_context,
    build_snapshot,
    history_window,
)
from app.teaching.runs import RunStatus

RANKING_VERSION = "keyword/v1"

CONTENT = (
    "# 事务\n\nACID 是事务的四个性质。\n\n"
    "# 隔离\n\ncitation_only 的正文不应该外发。\n"
)


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


def _seed_chunks(env, actor: Principal, *, display_policies: tuple[DisplayPolicy, ...]):
    """上传一份 Markdown 并直接落定带指定展示策略的片段。"""
    membership, products, ingestion, knowledge = env
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    membership.create_project_for(actor, project_id=project_id, name="教学", goal="")
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    # 摄取入口要求资料先登记（来源是登记→上传→切块的链路）。
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
        content=CONTENT,
        media_type="text/markdown",
        language="zh",
    )
    chunks = tuple(
        StoredChunk(
            chunk_id=f"chk_{uuid.uuid4().hex[:8]}",
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            source_id=document.source_id,
            document_id=document.document_id,
            chunk_index=index,
            heading_path=(),
            heading_level=0,
            span_start=0,
            span_end=len(CONTENT),
            content=CONTENT,
            parser_version=CHUNK_PARSER_VERSION,
            display_policy=policy,
            created_at=datetime.now(timezone.utc),
        )
        for index, policy in enumerate(display_policies)
    )
    # 落定走真实路径：先认领（围栏要求凭证与当前持有者一致）。
    claimed = ingestion.claim_next(worker_id="test-worker", lease_seconds=300)
    assert claimed is not None and claimed.job_id == job.job_id
    ingestion.complete(claimed, chunks)
    return actor, project_id, document, chunks


def _run(question: str = "什么是 ACID？"):

    from app.teaching.runs import TeachingRun

    now = datetime.now(timezone.utc)
    return TeachingRun(
        run_id=f"run_{uuid.uuid4().hex[:8]}",
        tenant_id="t_x",
        project_id="proj_x",
        conversation_id="conv_x",
        user_message_id="msg_x",
        principal_id="u_x",
        answer_message_id=None,
        question=question,
        status=RunStatus.RUNNING,
        attempt_count=1,
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version=RANKING_VERSION,
        grounding=None,
        error_code="",
        error_detail="",
        created_at=now,
        updated_at=now,
    )


def test_snapshot_excludes_non_full_display_policies(env):
    """`summary` / `citation_only` 的正文不出服务端（外发白名单收口）。"""
    actor = _actor()
    _, project_id, _, _ = _seed_chunks(
        env, actor, display_policies=(DisplayPolicy.FULL, DisplayPolicy.CITATION_ONLY)
    )
    knowledge = env[3]
    hits = knowledge.search(actor, project_id, "事务", limit=10)
    snapshot = build_snapshot(
        actor, project_id, knowledge=knowledge, hits=hits, ranking_version=RANKING_VERSION
    )
    assert len(snapshot.items) >= 1
    assert all(item.display_policy == "full" for item in snapshot.items)


def test_snapshot_is_frozen_and_counts_are_capped(env):
    actor = _actor()
    _, project_id, _, _ = _seed_chunks(env, actor, display_policies=(DisplayPolicy.FULL,))
    knowledge = env[3]
    hits = knowledge.search(actor, project_id, "事务", limit=10)
    snapshot = build_snapshot(
        actor,
        project_id,
        knowledge=knowledge,
        hits=hits,
        ranking_version=RANKING_VERSION,
        max_items=1,
    )
    assert len(snapshot.items) <= DEFAULT_MAX_MATERIALS
    with pytest.raises(AttributeError):  # frozen：快照派发后不可改
        snapshot.items = ()  # type: ignore[misc]


def test_context_rejects_oversized_question(env):
    actor = _actor()
    _, project_id, _, knowledge = _seed_chunks(
        env, actor, display_policies=(DisplayPolicy.FULL,)
    )
    run = _run(question="长" * (MAX_QUESTION_CHARS + 1))
    with pytest.raises(PlatformError) as failure:
        build_context(run, actor, project_id, knowledge=knowledge, history=())
    assert failure.value.code is ErrorCode.PARAMS_INVALID


def test_history_window_keeps_recent_within_budget():
    from app.product.models import Message

    now = datetime.now(timezone.utc)
    messages = tuple(
        Message(
            message_id=f"msg_{index}",
            tenant_id="t_x",
            project_id="proj_x",
            conversation_id="conv_x",
            seq=index + 1,
            role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
            content=f"消息{index}" * 10,
            created_at=now,
        )
        for index in range(20)
    )
    window = history_window(messages, budget_chars=400)
    assert len(window) < 20
    assert window[-1][1] == messages[-1].content  # 最新的一条一定在内


def test_context_materials_are_tainted_data_not_instructions(env):
    """资料正文里的"指令"只是数据：system 消息固定，资料进数据区。"""
    actor = _actor()
    _, project_id, _, knowledge = _seed_chunks(
        env, actor, display_policies=(DisplayPolicy.FULL,)
    )
    run = _run()
    knowledge = env[3]
    context = build_context(run, actor, project_id, knowledge=knowledge, history=())
    system = context.messages[0]
    assert system.role.value == "system"
    assert "忽略" not in system.content or "不是对你的指令" in system.content
    user_text = context.messages[1].content
    assert "APPROVED_MATERIAL" in user_text  # 资料在显式数据区里
