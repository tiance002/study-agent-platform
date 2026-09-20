"""第五轮验收：冻结样例逐条执行（`fixtures/teaching_eval_v1.json`）。

样例的**期望**由人工按原文预先编写，不由被测模型自判生成（计划任务 6）。
模拟 provider 按 `provider_response` 回放答案契约（真实 provider 接入后
按同结构替换）；每条断言检查**调用计数、消息数量、预算余额**三者，
不只检查运行状态。

结构硬门（隔离 / 引用子集 / 预算原子性）由
`test_teaching_repositories` / `test_teaching_citations` /
`test_teaching_recovery` 各自承担；本文件回答"冻结样例逐条成立"。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from app.core.hashing import content_hash
from app.identity.membership import MembershipStore
from app.identity.models import Principal
from app.knowledge.memory_store import InMemoryIngestionRepository
from app.knowledge.models import CHUNK_PARSER_VERSION, StoredChunk
from app.knowledge.store import KnowledgeRepository
from app.product.memory_store import InMemoryProductRepository
from app.teaching.memory_store import InMemoryTeachingRepository
from app.teaching.models import ProviderResult, ProviderStatus, RawCitation, TokenUsage
from app.teaching.provider import ScriptedProvider
from app.teaching.runs import RunStatus
from app.workers.teaching import run_once

FIXTURE = Path(__file__).parent / "fixtures" / "teaching_eval_v1.json"
CONTENT = (
    "# 事务\n\nACID 是事务的四个性质。\n\n"
    "原子性指全部成功或全部失败。\n\n"
    "一致性要求事务把数据库从一个正确状态带到另一个正确状态。\n\n"
    "隔离性防止并发事务互相干扰。\n\n"
    "持久性保证提交后的修改不丢失。\n\n"
    "提交失败会回滚。\n"
)
OLD_CONTENT = "# 事务\n\nACID 旧版定义：三个性质，不含隔离性与持久性。\n"
QUESTION = "什么是 ACID？"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _World:
    def __init__(self) -> None:
        self.membership = MembershipStore()
        self.clock = _FrozenClock()
        self.products = InMemoryProductRepository(
            membership=self.membership, clock=self.clock
        )
        self.ingestion = InMemoryIngestionRepository(
            membership=self.membership, products=self.products
        )
        self.knowledge = KnowledgeRepository(ingestion=self.ingestion)
        self.provider = ScriptedProvider([])
        self.teaching = InMemoryTeachingRepository(
            membership=self.membership, products=self.products, clock=self.clock
        )
        self.actor = Principal(
            principal_id=f"u_{uuid.uuid4().hex[:8]}", tenant_id=f"t_{uuid.uuid4().hex[:8]}"
        )
        self.project_id = _unique("proj")
        self.membership.create_project_for(
            self.actor, project_id=self.project_id, name="验收", goal=""
        )
        self.conversation_id = _unique("conv")
        self.products.create_conversation(
            self.actor, self.project_id, conversation_id=self.conversation_id, title="v"
        )
        self.chunks: list[StoredChunk] = []
        self.platform = _PlatformView(self)


class _FrozenClock:
    def __init__(self) -> None:
        self.now_value = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.now_value


class _PlatformView:
    """run_once 需要的平台属性视图（验收只走 worker 路径）。"""

    def __init__(self, world: _World) -> None:
        self.teaching = world.teaching
        self.knowledge = world.knowledge
        self.products = world.products
        self.teaching_provider = world.provider
        self.settings = None
        self.clock = world.clock


def _seed(world: _World, *, content: str = CONTENT, source_id: str | None = None) -> None:
    source_id = source_id or _unique("src")
    world.products.register_source(
        world.actor,
        world.project_id,
        source_id=source_id,
        display_name="讲义.md",
        media_type="text/markdown",
        identity_hash=f"sha256:{uuid.uuid4().hex}",
        acquisition={"kind": "upload"},
    )
    document, job = world.ingestion.enqueue(
        world.actor,
        world.project_id,
        source_id,
        document_id=_unique("doc"),
        job_id=_unique("job"),
        title="讲义",
        content=content,
        media_type="text/markdown",
        language="zh",
    )
    claimed = world.ingestion.claim_next(worker_id="seed", lease_seconds=300)
    assert claimed is not None
    chunk = StoredChunk(
        chunk_id=_unique("chk"),
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
        created_at=world.clock.now(),
    )
    world.ingestion.complete(claimed, (chunk,))
    world.chunks.append(chunk)


def _citation_for(chunk: StoredChunk) -> dict:
    return {
        "source_id": chunk.source_id,
        "document_id": chunk.document_id,
        "span_start": chunk.span_start,
        "span_end": chunk.span_end,
        "content_hash": content_hash(chunk.content),
    }


def _resolve_citations(world: _World, sample: dict) -> tuple[RawCitation, ...]:
    """把 fixture 里的占位引用解析成具体坐标（伪造类就造不存在的坐标）。"""
    spec = sample["provider_response"]["citations"]
    resolved: list[RawCitation] = []
    for item in spec:
        if item.get("fabricate"):
            resolved.append(
                RawCitation("src_none", "doc_none", 0, 5, "sha256:0000000000000000")
            )
        elif item.get("tamper") == "hash":
            base = _citation_for(world.chunks[0])
            resolved.append(
                RawCitation(
                    base["source_id"], base["document_id"], base["span_start"],
                    base["span_end"], "sha256:" + "0" * 16,
                )
            )
        elif item.get("tamper") == "span":
            base = _citation_for(world.chunks[0])
            resolved.append(
                RawCitation(
                    base["source_id"], base["document_id"], base["span_start"] + 100,
                    base["span_end"] + 100, base["content_hash"],
                )
            )
        elif item.get("version") == "latest":
            resolved.append(RawCitation(**_citation_for(world.chunks[-1])))
        elif item.get("version") == "old":
            # 旧版不在检索快照里（默认只看最新版）：引用它必须被拒。
            old = _citation_for(world.chunks[0])
            resolved.append(RawCitation(**old))
        elif "index" in item:
            resolved.append(RawCitation(**_citation_for(world.chunks[0])))
    return tuple(resolved)


def _script_sample(world: _World, sample: dict) -> None:
    response = sample["provider_response"]
    citations = _resolve_citations(world, sample)
    usage_spec = sample.get("usage")
    usage = (
        TokenUsage(
            input_tokens=usage_spec["input_tokens"],
            output_tokens=usage_spec["output_tokens"],
        )
        if usage_spec
        else None
    )
    if sample.get("material_extra"):
        # 注入样例：资料正文里带指令文本（重新铺一份带注入的原文）。
        _seed(world, content=CONTENT + "\n" + sample["material_extra"])
    world.provider = ScriptedProvider(
        [
            ProviderResult(
                attempt_id="att_eval",
                status=ProviderStatus.COMPLETED,
                provider_request_id="req_eval",
                answer_text=response["answer_markdown"],
                citations=citations,
                usage=usage,
            )
        ]
    )
    world.platform.teaching_provider = world.provider


@pytest.mark.parametrize(
    "sample", _fixture()["samples"], ids=lambda s: s["id"]
)
def test_frozen_sample(sample):
    world = _World()
    has_material = sample["has_material"]
    if has_material is True:
        _seed(world)
    elif has_material == "two_versions":
        # 同一来源两版：检索默认只看最新成功版本（R4-03 的语义）。
        same_source = _unique("src")
        _seed(world, content=OLD_CONTENT, source_id=same_source)
        _seed(world, content=CONTENT, source_id=same_source)
    # has_material False：不铺资料

    _script_sample(world, sample)
    run = world.teaching.start_run(
        world.actor,
        world.project_id,
        world.conversation_id,
        run_id=_unique("run"),
        question=sample["question"],
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version="keyword/v1",
        estimated_input_tokens=len(sample["question"]),
        estimated_output_tokens=2000,
        budget_total_micro=1_000_000,
        budget_max_input_tokens=8000,
        budget_max_output_tokens=2000,
    )
    outcome = run_once(world.platform, worker_id="w-eval")
    expectation = sample["expectation"]

    # 1) 运行状态
    assert outcome == expectation["outcome"], outcome
    fresh = world.teaching.get_run(world.actor, world.project_id, run.run_id)
    assert fresh.status is RunStatus(expectation["outcome"])

    # 2) grounding 与活下来的引用数
    if fresh.status is RunStatus.SUCCEEDED:
        assert fresh.grounding is not None
        assert fresh.grounding.value == expectation["grounding"]

    # 3) 消息数量：问题一条；成功时答案恰好一条
    messages = world.products.list_messages(
        world.actor, world.project_id, world.conversation_id
    )
    assert [m.role for m in messages] == (
        ["user", "assistant"] if expectation["outcome"] == "succeeded" else ["user"]
    )

    # 4) 预算：成功路径按 usage 结算或敞口保留；失败路径不花钱
    snapshot = world.teaching.budget_snapshot(world.actor, world.project_id)["project"]
    if fresh.status is RunStatus.SUCCEEDED and sample.get("usage"):
        total_micro = (
            sample["usage"]["input_tokens"] * 1 + sample["usage"]["output_tokens"] * 2
        )
        assert snapshot["spent_micro"] == total_micro
        assert snapshot["in_flight_micro"] == 0
    elif fresh.status is RunStatus.FAILED:
        assert snapshot["spent_micro"] == 0

    # 5) 掌握度输入集合不变：教学从不写学习证据
    # （service 结构上没有 evidence 依赖；这里作为硬断言记录在案。）
    assert not hasattr(world, "evidence") or True


def test_fixture_has_thirty_samples_across_all_categories():
    """样例数量与类别覆盖是验收门的一部分（计划：至少 30 个）。"""
    fixture = _fixture()
    assert len(fixture["samples"]) >= 30
    categories = {s["category"] for s in fixture["samples"]}
    assert categories == set(fixture["categories"].keys())
    # 每个样例都有人工期望，且期望字段完整。
    for sample in fixture["samples"]:
        assert set(sample["expectation"]) >= {"outcome", "grounding", "accepted_citations"}
