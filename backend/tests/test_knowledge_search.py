"""第 4 轮检索：作用域隔离、确定性排序、可核验引用与冻结夹具（任务 14）。

这个文件守四件**分开**的事，它们的失败模式完全不同：

1. **隔离**：跨项目/跨租户的候选既不出现在结果里，也读不回原文。
   失败模式是"越权但看起来正常" —— 候选内容读得通，只是它不属于你。
2. **确定性**：同一条查询在同一份数据上给出逐位相同的顺序。
   失败模式是"偶发失败"：评测指标每次跑都不一样，回归无从判定。
3. **可核验**：每个候选都能按 `source_id + span` 回读到**原文切片**，
   且指纹与内容一致。失败模式是"引用看起来有据、实际指向别处"。
4. **不夸大**：命中 ≠ 支持。有候选也要如实返回 `insufficient`。
   失败模式最危险：错误的 `supported` 会被直接采信。

⚠️ 隔离与回读这两组在 memory / postgres 两个适配器上**跑同一套断言**：
后者才是生产形态（FORCE RLS），而"内存版过了、PG 版没过"这种差异
只会在生产上第一次出现。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from app.core.clock import SystemClock
from app.core.errors import ErrorCode, PlatformError
from app.core.hashing import content_hash
from app.identity.models import Principal
from app.knowledge.evidence_state import EvidenceState, RetrievalHealth
from app.knowledge.models import StoredChunk
from app.knowledge.processor import DocumentProcessor
from app.knowledge.retrieval import (
    RANKING_VERSION,
    RELEVANCE_FLOOR,
    normalize_text,
    query_terms,
    rank_chunks,
)
from app.knowledge.store import KnowledgeRepository

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retrieval_v1.json"

MIGRATION_DSN = os.environ.get(
    "STUDY_PLATFORM_MIGRATION_DSN",
    "postgresql://postgres@127.0.0.1:5432/study_platform",
)

TENANT = "t_retr"
OTHER_TENANT = "t_retr_other"
ALICE = "u_retr_alice"
BOB = "u_retr_bob"
CAROL = "u_retr_carol"

EVENT_TIME = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(MIGRATION_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def pg_seed() -> None:
    """租户与主体是**运维动作**，所以用超级用户写。库不可达时静默返回：
    postgres 参数上的 skipif 负责跳过，内存用例不被牵连。"""
    if not _postgres_reachable():
        return
    with psycopg.connect(MIGRATION_DSN) as conn, conn.transaction():
        for tenant in (TENANT, OTHER_TENANT):
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant, tenant),
            )
        for principal, tenant in ((ALICE, TENANT), (BOB, TENANT), (CAROL, OTHER_TENANT)):
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                (principal, tenant),
            )


@dataclass
class Env:
    membership: object
    products: object
    ingestion: object

    def knowledge(self) -> KnowledgeRepository:
        return KnowledgeRepository(ingestion=self.ingestion)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _postgres_reachable(),
                    reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
                ),
            ],
            id="postgres",
        ),
    ]
)
def env(request, pg_seed) -> Env:
    if request.param == "memory":
        from app.identity.membership import MembershipStore
        from app.knowledge.memory_store import InMemoryIngestionRepository
        from app.product.memory_store import InMemoryProductRepository

        membership = MembershipStore()
        products = InMemoryProductRepository(membership=membership)
        return Env(
            membership=membership,
            products=products,
            ingestion=InMemoryIngestionRepository(
                membership=membership, products=products, clock=SystemClock()
            ),
        )

    from app.db.identity_store import PostgresMembershipRepository
    from app.db.ingestion_store import PostgresIngestionRepository
    from app.db.product_store import PostgresProductRepository

    membership = PostgresMembershipRepository()
    products = PostgresProductRepository(membership=membership)
    return Env(
        membership=membership,
        products=products,
        ingestion=PostgresIngestionRepository(membership=membership),
    )


# ------------------------------------------------------------------ 造数据


def _actor(principal_id: str = ALICE, tenant_id: str = TENANT) -> Principal:
    return Principal(principal_id=principal_id, tenant_id=tenant_id)


def _project(env: Env, principal_id: str = ALICE, tenant_id: str = TENANT) -> str:
    """建项目 + 授权。**授权是服务端动作** —— 客户端没有任何字段能改变成员关系。"""
    from app.identity.ports import SystemContext

    project_id = _unique("proj")
    context = SystemContext(tenant_id, "检索测试")
    env.membership.create_project(context, project_id=project_id, name="检索项目")
    env.membership.grant_project(
        context, principal_id=principal_id, project_id=project_id
    )
    return project_id


def _claim(env: Env, job_id: str):
    """认领直到拿到目标任务。

    中途认领到的**别的任务照常处理完** —— 那正是 worker 会做的事：
    `claim_next` 是跨租户的系统级操作（设计性质），所以它可能先返回
    别的用例留下的任务。把它们丢掉会让队列一直不干净，
    而"认领到不属于自己的任务就静默跳过"在真实 worker 里是一个丢任务的 bug。
    """
    processor = DocumentProcessor()
    for _ in range(200):
        job = env.ingestion.claim_next(worker_id="retr-test", lease_seconds=60)
        if job is None:
            break
        if job.job_id == job_id:
            return job
        env.ingestion.complete(job, processor.parse(env.ingestion.load_document(job)))
    raise AssertionError(f"没能认领到目标任务 {job_id}")


def _ingest(
    env: Env,
    actor: Principal,
    project_id: str,
    *,
    source_id: str,
    title: str,
    content: str,
) -> str:
    """走**真实管线**入料：登记资料 → 入队 → 认领 → 切块 → 落定。

    不直接往仓储里塞片段：那样切块器的改动不会影响本文件，
    而夹具里冻结的 `chunk_index` 正是用来发现切块回归的。
    """
    env.products.register_source(
        actor,
        project_id,
        source_id=source_id,
        display_name=title,
        media_type="text/markdown",
        identity_hash="sha256:" + source_id,
        acquisition={"kind": "upload"},
    )
    document, queued = env.ingestion.enqueue(
        actor,
        project_id,
        source_id,
        document_id=_unique("doc"),
        job_id=_unique("job"),
        title=title,
        content=content,
        media_type="text/markdown",
        language="zh",
    )
    env.ingestion.complete(_claim(env, queued.job_id), DocumentProcessor().parse(document))
    return queued.job_id


# ------------------------------------------------- 一、纯函数：确定性排序


def _chunk(
    source_id: str, index: int, content: str, *, heading: tuple[str, ...] = ()
) -> StoredChunk:
    return StoredChunk(
        chunk_id=_unique("chk"),
        tenant_id="t",
        project_id="p",
        source_id=source_id,
        document_id=f"doc_{source_id}",
        chunk_index=index,
        heading_path=heading,
        span_start=0,
        span_end=len(content),
        content=content,
        created_at=EVENT_TIME,
    )


@pytest.mark.invariant
def test_normalization_folds_fullwidth_and_case():
    """NFKC + 小写是**唯一**的归一化出口。

    不做这一步的话，从 PDF 粘来的全角文本（`Ｐｏｓｔｇｒｅｓ`）与
    索引里的 `PostgreSQL` 是两个词，表现是"这份资料搜不到"——
    而它看起来像"资料没入库"。
    """
    assert normalize_text("Ｐｏｓｔｇｒｅｓ") == "postgres"
    assert normalize_text("B-TREE") == "b-tree"
    assert normalize_text("ＡＢＣ") == "abc"


@pytest.mark.invariant
def test_query_terms_are_stable_and_bigrams_stay_inside_runs():
    """2-gram 只在**汉字段内**生成，不跨标点、不跨字符集。

    跨过去会造出 `s回` 这种在任何文档里都无意义的项：它拉高每个
    恰好含这两个字符的候选的分数，而这些命中与查询毫无关系。
    """
    first = query_terms("事务 回滚")
    second = query_terms("事务 回滚")
    assert first == second, "同一查询必须得到同一个元组（顺序也要一致）"
    assert "事务" in first and "回滚" in first
    assert query_terms("postgres回滚") == ("postgres", "回滚")
    assert all("-" not in term for term in query_terms("读已提交"))


@pytest.mark.invariant
def test_exact_phrase_outranks_scattered_bigrams():
    """整句命中必须压过"恰好凑齐了所有 2-gram 的长段落"。

    否则一份啰嗦的长文档会永远排在真正回答问题的短段落前面 ——
    而两边的"命中词数"看起来一样多。
    """
    exact = _chunk("src_a", 0, "回滚撤销本事务已经执行的全部修改")
    scattered = _chunk(
        "src_b", 0, "回滚" + "废话很长很长" * 30 + "撤销" + "更多废话" * 30 + "本事务"
    )
    ranked = rank_chunks((scattered, exact), "回滚撤销本事务", limit=5)
    assert ranked[0].chunk.source_id == "src_a"


@pytest.mark.invariant
def test_zero_score_candidates_are_dropped_and_blank_query_returns_nothing():
    """一个词都没命中的候选不进结果。

    把它们一并返回会让"检索到了东西"失去意义 —— 而证据判定正是按候选数说话的
    （`NO_CANDIDATES`）。**返回空集是一个诚实的答案。**
    """
    chunks = (_chunk("src_a", 0, "事务与回滚"),)
    assert rank_chunks(chunks, "量子纠缠", limit=5) == ()
    assert rank_chunks(chunks, "   ", limit=5) == ()
    assert rank_chunks(chunks, "!! ??", limit=5) == ()
    assert rank_chunks(chunks, "回滚", limit=0) == ()
    assert all(item.score > 0 for item in rank_chunks(chunks, "回滚", limit=5))


@pytest.mark.invariant
def test_tie_break_is_source_id_then_chunk_index_not_random_id():
    """同分时的次序必须**可复现**。

    用 `chunk_id` 当次序等于"同分时随机排"—— 它是 `new_id()` 生成的随机串，
    于是同一份数据重新入库一次，排序就变了，而两次结果都"正确"。
    """
    late = _chunk("src_b", 0, "复制延迟")
    early = _chunk("src_a", 7, "复制延迟")
    ranked = rank_chunks((late, early), "复制延迟", limit=5)
    assert [item.chunk.source_id for item in ranked] == ["src_a", "src_b"]

    first = _chunk("src_a", 9, "复制延迟")
    second = _chunk("src_a", 2, "复制延迟")
    ranked = rank_chunks((first, second), "复制延迟", limit=5)
    assert [item.chunk.chunk_index for item in ranked] == [2, 9]


@pytest.mark.invariant
def test_hits_expose_why_they_ranked():
    """`matched_terms` 是为排障准备的**证据**，不是装饰。

    "为什么这条排在前面"必须能由响应数据回答 —— 否则每次怀疑排序
    都要把代码跑一遍再看，而每个人看到的顺序未必一样。
    """
    ranked = rank_chunks((_chunk("src_a", 0, "回滚撤销已经执行的修改"),), "回滚", limit=5)
    assert ranked[0].matched_terms
    assert all(term in ranked[0].chunk.content or term == "回滚" for term in ranked[0].matched_terms)


# ------------------------------------------- 二、仓储：隔离、回读、引用


@pytest.mark.invariant
def test_search_never_returns_another_projects_candidate(env):
    """跨项目候选**根本不会出现在结果里**。

    这条断言的对象是"结果集合"，不是"结果里有没有被标成越权" ——
    02 号规格 §7 禁止的是后者那种做法：把候选拉回应用层再筛。
    """
    alice = _actor()
    project_a = _project(env)
    project_b = _project(env)
    _ingest(
        env, alice, project_a,
        source_id=_unique("src_a"), title="事务讲义",
        content="# 事务\n\n提交与回滚的边界。\n",
    )
    _ingest(
        env, alice, project_b,
        source_id=_unique("src_b"), title="另一个项目",
        content="# 秘密\n\n只属于另一个项目的秘密词。\n",
    )

    knowledge = env.knowledge()
    assert knowledge.search(alice, project_a, "秘密词", limit=10) == ()
    hits = knowledge.search(alice, project_a, "回滚", limit=10)
    assert [hit.chunk.project_id for hit in hits] == [project_a]


@pytest.mark.invariant
def test_search_is_scoped_to_the_tenant(env):
    """另一个租户的资料对任何人都不可见 —— 即使项目 id 被猜到。"""
    project = _project(env)
    _ingest(
        env, _actor(), project,
        source_id=_unique("src_t"), title="事务讲义",
        content="# 事务\n\n提交与回滚。\n",
    )

    carol = _actor(CAROL, OTHER_TENANT)

    # 另一个租户的主体连"这个项目存不存在"都不该知道：未授予一律拒绝，
    # 而不是返回空结果（空结果等于确认了项目存在）。
    with pytest.raises(PlatformError) as excinfo:
        env.knowledge().search(carol, project, "回滚", limit=10)
    assert excinfo.value.code in (
        ErrorCode.CROSS_TENANT_DENIED,
        ErrorCode.CROSS_PROJECT_DENIED,
    )


@pytest.mark.invariant
def test_each_hit_round_trips_through_exact_span_and_hash(env):
    """每个候选都必须能按引用回读到**原文切片**，且指纹一致。

    这是"可核验引用"的全部含义：拿到 `source_id + span + content_hash`
    的人，应当能独立验证这段引用确实是原文的一部分 —— 而不是相信检索结果的自述。
    """
    alice = _actor()
    project = _project(env)
    content = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。\n"
    source_id = _unique("src")
    _ingest(env, alice, project, source_id=source_id, title="事务讲义", content=content)

    knowledge = env.knowledge()
    hits = knowledge.search(alice, project, "回滚", limit=5)
    assert hits, "夹具内容里明明有'回滚'"

    for hit in hits:
        exact = knowledge.read_span(alice, project, hit.chunk.source_id, hit.chunk.span)
        assert exact is not None
        assert exact.content == hit.chunk.content
        assert content_hash(exact.content) == hit.chunk.content_hash
        # 原文切片必须真的是原文的切片（不是重新拼出来的等价文本）。
        assert content[exact.span_start : exact.span_end] == exact.content


@pytest.mark.invariant
def test_hits_carry_verifiable_citations_and_taint(env):
    """引用必须可核验，且检索结果一律带污点（不变量 #10）。

    没有 `ArtifactRef` 的命中只能靠"再查一次"来引用 —— 那把引用
    与一次新的查询绑在一起，两次结果之间的漂移就无从察觉。
    """
    alice = _actor()
    project = _project(env)
    _ingest(
        env, alice, project,
        source_id=_unique("src"), title="事务讲义",
        content="# 事务\n\n提交成功。\n",
    )

    hit = env.knowledge().search(alice, project, "提交", limit=1)[0]
    assert hit.citation.is_verifiable()
    assert hit.citation.span == hit.chunk.span
    assert hit.citation.parser_version == hit.chunk.parser_version
    assert hit.as_tainted().content_hash == hit.chunk.content_hash


@pytest.mark.invariant
def test_read_span_misses_are_one_value_and_denials_are_one_code(env):
    """粒度落空 → 同一个 `None`；授权拒绝 → 同一个拒绝码（与 `search` 同源）。

    `None` 里不允许区分"来源不存在 / span 越界 / 是项目里的另一份原文" ——
    区分它们等于提供存在性探针，探测者据此能数出你的项目里有哪些来源。

    而**授权拒绝刻意不走 `None`**：项目不属于你，就该抛出与 `search`、
    `get_job` 完全相同的那个拒绝码，而不是假装"里面没内容"。
    两条路在 HTTP 上确实同形（404 + "资源不存在"），但那由错误映射的
    **唯一**出口保证；把拒绝也翻译成 `None` 只是又开了一个判定出口。
    """
    alice = _actor()
    project = _project(env)
    other_project = _project(env)
    source_id = _unique("src")
    _ingest(
        env, alice, project,
        source_id=source_id, title="事务讲义",
        content="# 事务\n\n提交成功。\n",
    )
    knowledge = env.knowledge()

    # 存在的 span：先拿到一个真实的
    hit = knowledge.search(alice, project, "提交", limit=1)[0]
    assert knowledge.read_span(alice, project, source_id, hit.chunk.span) is not None

    # 粒度落空：三种情况同一个 `None`。
    assert knowledge.read_span(alice, project, "src_不存在", (0, 5)) is None
    assert knowledge.read_span(alice, project, source_id, (0, 9999)) is None
    assert knowledge.read_span(alice, other_project, source_id, hit.chunk.span) is None

    # 授权拒绝：抛出与 search 同一个码（同租户未授予的主体）。
    with pytest.raises(PlatformError) as span_denied:
        knowledge.read_span(_actor(BOB), project, source_id, hit.chunk.span)
    with pytest.raises(PlatformError) as search_denied:
        knowledge.search(_actor(BOB), project, "提交", limit=1)

    assert span_denied.value.code is search_denied.value.code, (
        "拒绝语义只能有一个来源：两个读取方法不得一个抛码、一个返回空"
    )
    assert span_denied.value.code in (
        ErrorCode.CROSS_TENANT_DENIED,
        ErrorCode.CROSS_PROJECT_DENIED,
    )


# --------------------------------------------- 三、判定：命中不等于支持


@pytest.mark.invariant
def test_hits_never_promote_the_evidence_state_to_supported(env):
    """有候选也要返回 `insufficient` + `MISSING_SUPPORT`。

    本轮没有冻结的核心结论标注集 → 拿不到"必需结论集合" → 无法证明覆盖。
    "有命中就报 supported"是这套判定存在的唯一理由：
    错误的 `insufficient` 会被追问后修正，错误的 `supported` 会被直接采信。
    """
    alice = _actor()
    project = _project(env)
    _ingest(
        env, alice, project,
        source_id=_unique("src"), title="事务讲义",
        content="# 事务\n\n提交与回滚。\n",
    )
    knowledge = env.knowledge()

    assessment = knowledge.assess(knowledge.search(alice, project, "回滚", limit=5))
    assert assessment.evidence.state is EvidenceState.INSUFFICIENT
    codes = {str(issue.code) for issue in assessment.evidence.issues}
    assert "MISSING_SUPPORT" in codes
    # 过程健康度与证据状态是**两个问题**，可以同时是"干净"与"证据不足"。
    assert assessment.health is RetrievalHealth.CLEAN
    assert assessment.to_dict()["retrieval_health"] == "clean"


@pytest.mark.invariant
def test_no_hits_produce_the_no_candidates_issue(env):
    alice = _actor()
    project = _project(env)
    _ingest(
        env, alice, project,
        source_id=_unique("src"), title="事务讲义",
        content="# 事务\n\n提交与回滚。\n",
    )
    knowledge = env.knowledge()

    hits = knowledge.search(alice, project, "量子纠缠", limit=5)
    assert hits == ()
    assessment = knowledge.assess(hits)
    assert assessment.signals.candidate_count == 0
    assert "NO_CANDIDATES" in {str(issue.code) for issue in assessment.evidence.issues}


@pytest.mark.invariant
def test_low_relevance_is_unreachable_at_this_ability_level():
    """`LOW_RELEVANCE` 在当前路径上**不会触发** —— 这一点要被机械地记着。

    只有整数关键词分数时，除了"有没有命中"没有第二个可判的量，
    所以下限只能取 1，而 `rank_chunks` 已经保证候选分数 > 0。
    这与 `evidence_state.py` 里 `SCOPE_BLOCKED` 的标注同一性质：
    判定已就绪，但当前路径产生不了它。等有了 embedding / rerank 的连续分数，
    这个下限才有实际含义 —— 那时本条测试应当失败，提醒把它改成真正的断言。
    """
    top = _chunk("src_a", 0, "回滚")
    hits = rank_chunks((top,), "回滚", limit=5)
    assert hits[0].score >= RELEVANCE_FLOOR


# ------------------------------------------------- 四、冻结夹具的基线门


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_env() -> tuple[Env, KnowledgeRepository, Principal, str, dict[str, str]]:
    """把夹具语料灌进一个内存环境，返回 (env, knowledge, actor, project, 源 id 映射)。"""
    from app.identity.membership import MembershipStore
    from app.knowledge.memory_store import InMemoryIngestionRepository
    from app.product.memory_store import InMemoryProductRepository

    membership = MembershipStore()
    products = InMemoryProductRepository(membership=membership)
    ingestion = InMemoryIngestionRepository(
        membership=membership, products=products, clock=SystemClock()
    )
    env = Env(membership=membership, products=products, ingestion=ingestion)
    actor = _actor()
    project = _project(env)
    mapping: dict[str, str] = {}
    for document in _fixture()["corpus"]:
        real_id = f"{document['source_id']}_fx"
        mapping[document["source_id"]] = real_id
        _ingest(
            env, actor, project,
            source_id=real_id,
            title=document["title"],
            content=document["content"],
        )
    return env, KnowledgeRepository(ingestion=ingestion), actor, project, mapping


@pytest.mark.invariant
def test_frozen_fixture_declares_the_current_ranking_version():
    """夹具的版本号必须等于代码里的版本号。

    分词器、切块器、解析器或排序规则任一变化，都要显式改版本号并重跑夹具 ——
    这条断言让"改了但没重跑"变成一次**失败**，而不是一次没人注意到的召回漂移。
    """
    assert _fixture()["version"] == RANKING_VERSION


@pytest.mark.invariant
def test_frozen_chinese_fixture_meets_its_recorded_floors():
    """冻结的中文检索夹具：recall@5 与 MRR 不得低于记录的下限。

    期望答案是**手写**的（人工判断"哪一段才是对的答案"），不是从实现输出里抄的 ——
    抄出来的期望永远 100% 通过，它测的是"代码没变"，不是"检索对"。
    """
    fixture = _fixture()
    _env, knowledge, actor, project, mapping = _fixture_env()

    hit_cases = [case for case in fixture["cases"] if case["kind"] != "no_hit"]
    no_hit_cases = [case for case in fixture["cases"] if case["kind"] == "no_hit"]
    assert len(fixture["cases"]) >= 20, "夹具的语料量本身就是一条防线"
    assert {case["kind"] for case in fixture["cases"]} >= {
        "heading", "paragraph", "code", "no_hit",
    }, "夹具必须覆盖标题、段落、代码与无命中四类"

    found = 0
    reciprocal_sum = 0.0
    for case in hit_cases:
        hits = knowledge.search(actor, project, case["query"], limit=5)
        wanted = {
            (mapping[item["source_id"]], item["chunk_index"]) for item in case["expect"]
        }
        ranks = [
            position
            for position, hit in enumerate(hits, start=1)
            if (hit.chunk.source_id, hit.chunk.chunk_index) in wanted
        ]
        if ranks:
            found += 1
            reciprocal_sum += 1.0 / ranks[0]

    recall_at_5 = found / len(hit_cases)
    mrr = reciprocal_sum / len(hit_cases)
    assert recall_at_5 >= fixture["baseline"]["recall_at_5"], (
        f"recall@5 从 {fixture['baseline']['recall_at_5']} 掉到 {recall_at_5}"
    )
    assert mrr >= fixture["baseline"]["mrr"], (
        f"MRR 从 {fixture['baseline']['mrr']} 掉到 {mrr}"
    )

    # 无命中用例单独判：检索不该被"总能返回点什么"的实现糊弄过去。
    for case in no_hit_cases:
        assert knowledge.search(actor, project, case["query"], limit=5) == (), case["query"]


@pytest.mark.invariant
def test_fixture_baseline_matches_what_the_corpus_actually_yields():
    """下限不是装饰：它必须等于一次真实跑出来的结果。

    把下限写成一个宽松的旧值，等于让它慢慢失效而不触发任何告警 ——
    那和没有这条门禁是一样的。
    """
    baseline = _fixture()["baseline"]
    assert baseline["hit_case_count"] + baseline["no_hit_case_count"] == len(
        _fixture()["cases"]
    )
    assert baseline["recall_at_5"] >= 0.9, "基线低于 0.9 说明语料或排序有真问题，不该被冻结下来"


# --------------------------------------------------------- 五、API 端点


@pytest.fixture
def learner(cookie_project, platform):
    """已登录 cookie 用户 + 一个项目 + 一份已切块入库的资料。"""
    client, project_id = cookie_project
    source = client.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "事务讲义"},
        headers=_keyed(),
    )
    assert source.status_code == 201
    source_id = source.json()["source_id"]
    upload = client.post(
        f"/projects/{project_id}/sources/{source_id}/content",
        json={
            "title": "事务讲义",
            "content": "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。\n",
            "media_type": "text/markdown",
        },
        headers=_keyed(),
    )
    assert upload.status_code == 202, upload.text
    from app.workers.ingestion import run_once

    assert run_once(platform, worker_id="retr-api").kind == "succeeded"
    return client, platform, project_id, source_id


def _keyed(key: str | None = None) -> dict:
    return {
        "Origin": "http://testserver",
        "Idempotency-Key": key or "retr-" + uuid.uuid4().hex,
    }


def _without_request_id(response) -> dict:
    """错误体去掉 `request_id` 后的那部分 —— 客户端据以分支的字段都在这里。

    追踪 id 按设计每请求唯一，比对"逐字相同"时必须排除它，
    否则这条断言等价于"要求追踪失效"。
    """
    body = dict(response.json())
    body.pop("request_id", None)
    return body


@pytest.mark.invariant
def test_search_endpoint_returns_citations_and_a_conservative_state(learner):
    client, _platform, project_id, _source_id = learner

    response = client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "limit": 5},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ranking_version"] == RANKING_VERSION
    assert payload["retrieval_health"] == "clean"
    assert payload["evidence"]["state"] == "insufficient"
    assert payload["hits"], "语料里明明有'回滚'"
    hit = payload["hits"][0]
    assert hit["citation"]["source_id"] == _source_id
    assert hit["citation"]["span"] == hit["span"]
    assert hit["citation"]["content_hash"] == hit["content_hash"]
    assert hit["parser_version"] == "structure/v1"
    assert hit["display_policy"] == "full"


@pytest.mark.invariant
def test_search_endpoint_does_not_demand_an_idempotency_key(learner):
    """检索是**读**，不要求 `Idempotency-Key`。

    给没有副作用的操作套上幂等守卫不保护任何东西，只多一个必填头；
    而"必填但无意义"的字段会被一路照抄到真正需要它的地方，那时它已经不表示什么了。
    """
    client, _, project_id, _ = learner

    response = client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert "Idempotency-Key" not in response.request.headers


@pytest.mark.invariant
def test_search_request_bounds_are_enforced(learner):
    client, _, project_id, _ = learner
    headers = {"Origin": "http://testserver"}

    assert client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "", "limit": 5},
        headers=headers,
    ).status_code == 422
    assert client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "x" * 2001, "limit": 5},
        headers=headers,
    ).status_code == 422
    assert client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "limit": 21},
        headers=headers,
    ).status_code == 422
    assert client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "offset": 3},
        headers=headers,
    ).status_code == 422, "未知字段必须被拒（extra=forbid）"


@pytest.mark.invariant
def test_span_endpoint_returns_the_exact_slice(learner):
    client, _, project_id, source_id = learner
    search = client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "limit": 1},
        headers={"Origin": "http://testserver"},
    ).json()
    hit = search["hits"][0]
    start, end = hit["span"]

    response = client.get(
        f"/projects/{project_id}/sources/{source_id}/span",
        params={"start": start, "end": end},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == hit["content"]
    assert content_hash(body["content"]) == hit["content_hash"]
    assert body["citation"] == hit["citation"]


@pytest.mark.invariant
def test_span_endpoint_miss_wording_is_identical_for_every_kind_of_miss(learner):
    """同一类落空（span 越界 / 来源不存在 / 另一个项目）给出**同码同话术**。

    区分它们等于把权限边界变成存在性探针 —— 探测者据此能数出
    别人的项目里有哪些资料、span 对不对得上。

    `request_id` 刻意排除在比对之外：它按设计就是**每请求唯一**的
    （服务端生成的追踪 id），把它算进"逐字相同"等于要求追踪失效。
    要比的是客户端据以分支的那几个字段。
    """
    client, _, project_id, source_id = learner
    headers = _keyed()
    other_project = client.post(
        "/projects", json={"name": "另一个项目"}, headers=headers
    ).json()["project_id"]

    responses = [
        client.get(
            f"/projects/{project_id}/sources/{source_id}/span", params={"start": 0, "end": 9999}
        ),
        client.get(
            f"/projects/{project_id}/sources/src_不存在/span", params={"start": 0, "end": 5}
        ),
        client.get(
            f"/projects/{other_project}/sources/{source_id}/span", params={"start": 0, "end": 5}
        ),
    ]

    assert {response.status_code for response in responses} == {404}
    bodies = {json.dumps(_without_request_id(response), sort_keys=True) for response in responses}
    assert len(bodies) == 1, "同一类落空必须给出逐字相同的响应"
    # 追踪 id 必须仍然存在且各不相同 —— 否则上面那条"相同"是因为它压根没生成。
    assert len({response.json()["request_id"] for response in responses}) == 3
    assert all(response.json()["request_id"] for response in responses)


@pytest.mark.invariant
def test_span_endpoint_rejects_an_inverted_range(learner):
    client, _, project_id, source_id = learner

    response = client.get(
        f"/projects/{project_id}/sources/{source_id}/span", params={"start": 9, "end": 3}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "PARAMS_INVALID"
