"""第五轮 PostgreSQL 端到端：真实数据库上的持久化、重启恢复与围栏。

与第四轮的 e2e 同一纪律：临时库（`conftest.pg_database` 会话级夹具）、
真实仓储、真实 worker 路径。**PG 参数被 skip 就等于这一关没过。**
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.core.hashing import content_hash
from app.db.identity_store import PostgresMembershipRepository
from app.db.ingestion_store import PostgresIngestionRepository
from app.db.product_store import PostgresProductRepository
from app.db.teaching_store import PostgresTeachingRepository
from app.identity.models import Principal
from app.knowledge.models import CHUNK_PARSER_VERSION, StoredChunk
from app.knowledge.store import KnowledgeRepository
from app.teaching.models import TokenUsage
from app.teaching.provider import ScriptedProvider, completed_result
from app.teaching.runs import Grounding, RunStatus
from app.workers.teaching import run_once

QUESTION = "什么是 ACID？"
CONTENT = "# 事务\n\nACID 是事务的四个性质。\n"

TENANT = "t_r5_pg"


def _drain() -> None:
    """排空遗留的运行与摄取任务（认领是跨租户的系统级操作）。"""
    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE teaching_runs SET status='failed', error_code='TEST_DRAIN',"
                " error_detail='测试前置', lease_owner=NULL, lease_until=NULL,"
                " claim_token=NULL WHERE status IN ('queued','running')"
            )
            conn.execute(
                "UPDATE ingestion_jobs SET status='failed', error_code='TEST_DRAIN',"
                " error_detail='测试前置', lease_owner=NULL, lease_until=NULL,"
                " claim_token=NULL WHERE status IN ('queued','processing')"
            )


@pytest.fixture(scope="module")
def pg_seed() -> None:
    if not pg_support.reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING",
                (TENANT, TENANT),
            )
    _drain()


class _Env:
    def __init__(self, *, tag: str) -> None:
        self.is_postgres = True
        self.membership = PostgresMembershipRepository()
        self.products = PostgresProductRepository(membership=self.membership)
        self.ingestion = PostgresIngestionRepository(membership=self.membership)
        self.knowledge = KnowledgeRepository(ingestion=self.ingestion)
        self.provider = ScriptedProvider([])
        self.teaching = PostgresTeachingRepository(membership=self.membership)
        self.platform = SimpleNamespace(
            teaching=self.teaching,
            knowledge=self.knowledge,
            products=self.products,
            teaching_provider=self.provider,
            settings=None,
            clock=None,
        )
        self.tenant = f"t_r5_{tag}_{uuid.uuid4().hex[:8]}"
        self.actor = Principal(
            principal_id=f"u_r5_{tag}_{uuid.uuid4().hex[:8]}", tenant_id=self.tenant
        )
        dsn = pg_support.require_test_database(pg_support.migration_dsn())
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (self.tenant, self.tenant),
                )
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (self.actor.principal_id, self.tenant),
                )
        self.project_id = f"proj_{uuid.uuid4().hex[:10]}"
        self.membership.create_project_for(
            self.actor, project_id=self.project_id, name="第五轮 e2e", goal=""
        )
        self.conversation_id = f"conv_{uuid.uuid4().hex[:10]}"
        self.products.create_conversation(
            self.actor, self.project_id, conversation_id=self.conversation_id, title="e2e"
        )

    def seed_chunk(self) -> StoredChunk:
        source_id = f"src_{uuid.uuid4().hex[:10]}"
        self.products.register_source(
            self.actor,
            self.project_id,
            source_id=source_id,
            display_name="讲义.md",
            media_type="text/markdown",
            identity_hash=f"sha256:{uuid.uuid4().hex}",
            acquisition={"kind": "upload"},
        )
        document, job = self.ingestion.enqueue(
            self.actor,
            self.project_id,
            source_id,
            document_id=f"doc_{uuid.uuid4().hex[:10]}",
            job_id=f"job_{uuid.uuid4().hex[:10]}",
            title="讲义",
            content=CONTENT,
            media_type="text/markdown",
            language="zh",
        )
        claimed = self.ingestion.claim_next(worker_id="seed", lease_seconds=300)
        assert claimed is not None
        chunk = StoredChunk(
            chunk_id=f"chk_{uuid.uuid4().hex[:10]}",
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            source_id=document.source_id,
            document_id=document.document_id,
            chunk_index=0,
            heading_path=(),
            heading_level=0,
            span_start=0,
            span_end=len(CONTENT),
            content=CONTENT,
            parser_version=CHUNK_PARSER_VERSION,
            created_at=datetime.now(timezone.utc),
        )
        self.ingestion.complete(claimed, (chunk,))
        return chunk


@pytest.mark.postgres
@pytest.mark.invariant
@pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_full_teaching_pipeline_on_postgres(pg_seed):
    env = _Env(tag="a")
    chunk = env.seed_chunk()
    env.platform.teaching_provider = env.provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "answer_markdown": "ACID 指四个性质。",
                    "citations": [
                        {
                            "source_id": chunk.source_id,
                            "document_id": chunk.document_id,
                            "span_start": 0,
                            "span_end": len(CONTENT),
                            "content_hash": content_hash(CONTENT),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    run = env.teaching.start_run(
        env.actor,
        env.project_id,
        env.conversation_id,
        run_id=f"run_{uuid.uuid4().hex[:12]}",
        question=QUESTION,
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version="keyword/v1",
        estimated_input_tokens=len(QUESTION),
        estimated_output_tokens=2000,
        budget_total_micro=1_000_000,
        budget_max_input_tokens=8000,
        budget_max_output_tokens=2000,
    )
    assert run_once(env.platform, worker_id="w-e2e") == "succeeded"

    messages = env.products.list_messages(env.actor, env.project_id, env.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    snapshot = env.teaching.budget_snapshot(env.actor, env.project_id)["project"]
    assert snapshot["spent_micro"] == 100 * 1 + 50 * 2  # ScriptedProvider 默认用量
    events = env.teaching.list_events(env.actor, env.project_id, run.run_id)
    assert [e.event_type for e in events] == [
        "run.created", "run.dispatched", "run.succeeded",
    ]


@pytest.mark.postgres
@pytest.mark.invariant
@pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_restart_preserves_runs_messages_events_and_budget(pg_seed):
    """重启（新建全部仓储实例）后：run、消息、事件、预算全部还在。"""
    env = _Env(tag="b")
    chunk = env.seed_chunk()
    env.platform.teaching_provider = env.provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "answer_markdown": "重启后仍在的回答。",
                    "citations": [
                        {
                            "source_id": chunk.source_id,
                            "document_id": chunk.document_id,
                            "span_start": 0,
                            "span_end": len(CONTENT),
                            "content_hash": content_hash(CONTENT),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    run = env.teaching.start_run(
        env.actor,
        env.project_id,
        env.conversation_id,
        run_id=f"run_{uuid.uuid4().hex[:12]}",
        question=QUESTION,
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version="keyword/v1",
        estimated_input_tokens=len(QUESTION),
        estimated_output_tokens=2000,
        budget_total_micro=1_000_000,
        budget_max_input_tokens=8000,
        budget_max_output_tokens=2000,
    )
    assert run_once(env.platform, worker_id="w1") == "succeeded"
    budget_before = env.teaching.budget_snapshot(env.actor, env.project_id)
    events_before = env.teaching.list_events(env.actor, env.project_id, run.run_id)

    # ---- "重启"：全新实例（同一数据库）----
    restarted = _Env.__new__(_Env)
    restarted.membership = PostgresMembershipRepository()
    restarted.products = PostgresProductRepository(membership=restarted.membership)
    restarted.ingestion = PostgresIngestionRepository(membership=restarted.membership)
    restarted.knowledge = KnowledgeRepository(ingestion=restarted.ingestion)
    restarted.teaching = PostgresTeachingRepository(membership=restarted.membership)

    fresh_run = restarted.teaching.get_run(env.actor, env.project_id, run.run_id)
    assert fresh_run.status is RunStatus.SUCCEEDED
    assert fresh_run.answer_message_id
    assert (
        restarted.teaching.budget_snapshot(env.actor, env.project_id) == budget_before
    )
    assert (
        restarted.teaching.list_events(env.actor, env.project_id, run.run_id)
        == events_before
    )
    messages = restarted.products.list_messages(
        env.actor, env.project_id, env.conversation_id
    )
    assert [m.role for m in messages] == ["user", "assistant"]


@pytest.mark.postgres
@pytest.mark.invariant
@pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_stale_claim_is_rejected_and_budget_settles_once(pg_seed):
    """A 的租约过期 → B 接管并完成 → A 迟到的落定被拒，费用只结算一次。"""
    env = _Env(tag="c")
    env.platform.teaching_provider = env.provider = ScriptedProvider(
        [completed_result(attempt_id="att_x", answer="新持有者的回答。")]
    )
    run = env.teaching.start_run(
        env.actor,
        env.project_id,
        env.conversation_id,
        run_id=f"run_{uuid.uuid4().hex[:12]}",
        question=QUESTION,
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version="keyword/v1",
        estimated_input_tokens=len(QUESTION),
        estimated_output_tokens=2000,
        budget_total_micro=1_000_000,
        budget_max_input_tokens=8000,
        budget_max_output_tokens=2000,
    )
    stale = env.teaching.claim_run(worker_id="w-old", lease_seconds=0)
    assert stale.run.run_id == run.run_id

    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE teaching_runs SET lease_until = now() - interval '1 second'"
                " WHERE run_id = %s",
                (run.run_id,),
            )

    fresh = env.teaching.claim_run(worker_id="w-new", lease_seconds=300)
    assert fresh.run.run_id == run.run_id

    attempt_id = "att_stale"
    env.teaching.mark_dispatched(
        fresh, attempt_id=attempt_id,
        estimated_input_tokens=len(QUESTION), estimated_output_tokens=2000,
    )
    with pytest.raises(PlatformError) as failure:
        env.teaching.finish_run(
            stale, attempt_id=attempt_id,
            answer_message_id=f"msg_{uuid.uuid4().hex[:10]}",
            answer_text="旧持有者的答案", grounding=Grounding.INFERENCE_ONLY,
            usage=None,
        )
    assert failure.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION

    # B 正常落定（阳性对照）：消息恰好一条，费用结算一次。
    finished = env.teaching.finish_run(
        fresh, attempt_id=attempt_id,
        answer_message_id=f"msg_{uuid.uuid4().hex[:10]}",
        answer_text="新持有者的回答。", grounding=Grounding.INFERENCE_ONLY,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    assert finished.status is RunStatus.SUCCEEDED
    messages = env.products.list_messages(env.actor, env.project_id, env.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    snapshot = env.teaching.budget_snapshot(env.actor, env.project_id)["project"]
    assert snapshot["spent_micro"] == 100 * 1 + 50 * 2
    assert snapshot["in_flight_micro"] == 0
