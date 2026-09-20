"""摄取仓储的**契约测试**：内存与 PostgreSQL 适配器跑同一批断言。

延续 `test_product_repositories.py` 的参数化模式。本文件重点守五类
"不会报错、只会慢慢分叉或静默出错"的语义：

1. **入队原子性**：原文与任务一起出现，版本号由实现分配且并发安全；
2. **认领的独占性**：并发 worker 不会拿到同一个任务（PG 靠 `SKIP LOCKED`）；
3. **片段作用域**：跨项目/跨文档的片段挂不上去（否则引用会指向别人的资料）；
4. **完成的幂等**：重复投递不追加第二套片段，也不报错；
5. **终态的收敛**：成功/失败之后不再被认领；租约过期才允许回收。

⚠️ PostgreSQL 参数被 skip 就等于这一关没过。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.knowledge.models import (
    CHUNK_PARSER_VERSION,
    IngestionStatus,
    SourceDocument,
    StoredChunk,
)
from app.product.models import SourceRecord

#: 整场会话跑在随机临时库上（`conftest.pg_database`，会话级 autouse）——
#: 本文件有跨租户的排空操作，绝不允许在业务库上执行。

TENANT = "t_ing_pg"
OTHER_TENANT = "t_ing_pg_other"
ALICE = "u_ing_pg_alice"
BOB = "u_ing_pg_bob"
CAROL = "u_ing_pg_carol"

CONTENT = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。"


def _drain_queue() -> None:
    """把遗留的未终态任务推进到终态，让每个用例都从空队列开始。

    为什么必须显式做这件事：`claim_next` 是**跨租户**的系统级操作（worker 要先
    发现"哪个租户有活干"），所以它拿回来的是全库最早的一条排队任务 ——
    包括上一次运行、上一个用例留下的。用超级用户连接直接写终态，
    是因为这一步要绕过 RLS 才能覆盖所有租户。

    ⚠️ **只能发生在临时测试库上**：这一句会终结目标库里所有在途任务，
    指向业务库时就是"跑一次测试，用户全部上传卡死"。所以先过
    `require_test_database`（库名不以 `study_test_` 开头直接拒绝），
    失败发生在**任何写入之前**。
    """
    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE ingestion_jobs"
                " SET status = 'failed', error_code = 'TEST_DRAIN',"
                "     error_detail = '测试前置：排空遗留任务',"
                "     lease_owner = NULL, lease_until = NULL"
                " WHERE status IN ('queued', 'processing')"
            )


@pytest.fixture(scope="module")
def pg_seed() -> None:
    """租户与主体用超级用户写（运维动作）。库不可达时静默返回：
    postgres 参数上的 skipif 负责跳过 PG 用例，内存用例不被牵连。"""
    if not pg_support.reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            for tenant in (TENANT, OTHER_TENANT):
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant, tenant),
                )
            for principal, tenant in (
                (ALICE, TENANT),
                (BOB, TENANT),
                (CAROL, OTHER_TENANT),
            ):
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


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not pg_support.reachable(),
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
                membership=membership, products=products
            ),
        )

    from app.db.identity_store import PostgresMembershipRepository
    from app.db.ingestion_store import PostgresIngestionRepository
    from app.db.product_store import PostgresProductRepository

    _drain_queue()
    membership = PostgresMembershipRepository()
    products = PostgresProductRepository(membership=membership)
    return Env(
        membership=membership,
        products=products,
        ingestion=PostgresIngestionRepository(membership=membership),
    )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _alice() -> Principal:
    return Principal(principal_id=ALICE, tenant_id=TENANT)


def _bob() -> Principal:
    return Principal(principal_id=BOB, tenant_id=TENANT)


def _register(env: Env, project_id: str, *, actor: Principal | None = None) -> SourceRecord:
    return env.products.register_source(
        actor or _alice(),
        project_id,
        source_id=_unique("src"),
        display_name="事务讲义.md",
        media_type="text/markdown",
        identity_hash="sha256:" + uuid.uuid4().hex,
        acquisition={"kind": "upload"},
    )


def _project(env: Env, *, actor: Principal | None = None) -> str:
    project_id = _unique("proj")
    env.membership.create_project_for(
        actor or _alice(), project_id=project_id, name="摄取契约", goal=""
    )
    return project_id


def _enqueue(env: Env, project_id: str, source_id: str, *, content: str = CONTENT):
    return env.ingestion.enqueue(
        _alice(),
        project_id,
        source_id,
        document_id=_unique("doc"),
        job_id=_unique("job"),
        title="事务讲义",
        content=content,
        media_type="text/markdown",
        language="zh",
    )


def _chunks_for(
    document: SourceDocument, slices: list[tuple[int, int]]
) -> tuple[StoredChunk, ...]:
    """按原文区间造片段。内容一律取自原文，保证 span 与内容严丝合缝。"""
    now = datetime.now(timezone.utc)
    return tuple(
        StoredChunk(
            chunk_id=_unique("chk"),
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            source_id=document.source_id,
            document_id=document.document_id,
            chunk_index=index,
            heading_path=(),
            heading_level=0,
            span_start=start,
            span_end=end,
            content=document.content[start:end],
            parser_version=CHUNK_PARSER_VERSION,
            created_at=now,
        )
        for index, (start, end) in enumerate(slices)
    )


# ------------------------------------------------------------------ 入队


@pytest.mark.invariant
def test_enqueue_writes_document_and_queued_job(env):
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)

    assert document.version == 1
    assert document.content == CONTENT
    assert document.content_hash.startswith("sha256:")
    assert document.taint_sources == ("uploaded_source",)
    assert job.status is IngestionStatus.QUEUED
    assert job.attempt_count == 0
    assert job.document_id == document.document_id
    assert env.ingestion.get_job(_alice(), project_id, job.job_id).job_id == job.job_id


@pytest.mark.invariant
def test_reupload_allocates_next_version_instead_of_overwriting(env):
    """同一份资料的第二次上传是**新版本**，不是改旧行。

    覆盖旧行会让所有已发出的引用指向另一段文字，而 `content_hash` 依然是旧的 ——
    引用"可核验"就此变成一句空话。
    """
    project_id = _project(env)
    source = _register(env, project_id)
    first, first_job = _enqueue(env, project_id, source.source_id)
    second, second_job = _enqueue(env, project_id, source.source_id, content="# 新版本")

    assert (first.version, second.version) == (1, 2)
    assert first_job.job_id != second_job.job_id
    # 旧版本仍按原样存在于库里。
    assert env.ingestion.load_document(first_job).content == CONTENT


@pytest.mark.invariant
def test_source_from_another_project_cannot_be_enqueued(env):
    """给别的项目的资料挂原文会被拒绝 —— 这是引用跨项目的入口。"""
    project_a = _project(env)
    project_b = _project(env, actor=_bob())
    source_b = _register(env, project_b, actor=_bob())

    with pytest.raises(PlatformError) as excinfo:
        _enqueue(env, project_a, source_b.source_id)
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED


# ------------------------------------------------------------------ 认领


@pytest.mark.invariant
def test_claim_marks_processing_and_holds_lease(env):
    project_id = _project(env)
    source = _register(env, project_id)
    _document, job = _enqueue(env, project_id, source.source_id)

    claimed = env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status is IngestionStatus.PROCESSING
    assert claimed.attempt_count == 1
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_until is not None

    # 另一个 worker 在没有新任务时拿不到任何东西 —— 而不是拿到同一个。
    assert env.ingestion.claim_next(worker_id="worker-b", lease_seconds=300) is None


@pytest.mark.invariant
def test_concurrent_workers_never_claim_the_same_job(env, racy_scheduling):
    """独占性必须实测。PG 版靠 `FOR UPDATE SKIP LOCKED`，
    内存版靠锁 —— 但"应该如此"要跑过才算数。"""
    project_id = _project(env)
    source = _register(env, project_id)
    jobs = {
        _enqueue(env, project_id, source.source_id)[1].job_id for _ in range(4)
    }

    barrier = threading.Barrier(4)
    results: list[object] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait()
        try:
            claimed = env.ingestion.claim_next(worker_id=name, lease_seconds=300)
            with lock:
                results.append(claimed.job_id if claimed else None)
        except Exception as exc:  # noqa: BLE001 — 收集全部异常用于断言
            with lock:
                results.append(exc)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(isinstance(item, str) for item in results), results
    assert len(set(results)) == 4, f"同一任务被认领多次：{results}"
    assert set(results) == jobs


@pytest.mark.invariant
def test_expired_lease_is_reclaimable_but_terminal_job_is_not(env):
    """崩溃残留（租约过期）必须可回收；终态任务必须不被回收。

    两条一起测：只测前者会漏掉"成功之后又被捞起来做一遍"，
    只测后者会漏掉"崩一次任务就永远卡住"。
    """
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)

    crashed = env.ingestion.claim_next(worker_id="crashed", lease_seconds=0)
    assert crashed is not None and crashed.job_id == job.job_id

    reclaimed = env.ingestion.claim_next(worker_id="worker-b", lease_seconds=300)
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id, "租约过期后应被重新认领"
    assert reclaimed.attempt_count == 2, "重试次数必须如实累积"
    assert reclaimed.lease_owner == "worker-b"

    env.ingestion.complete(reclaimed, _chunks_for(document, [(0, 5)]))
    assert env.ingestion.claim_next(worker_id="worker-c", lease_seconds=300) is None, (
        "succeeded 是终态，不得被重新认领"
    )


# ------------------------------------------------------------------ 完成


@pytest.mark.invariant
def test_cross_project_chunks_cannot_be_attached(env):
    """把一个项目的片段挂到另一个项目的任务上必须被拒绝。

    少了这道判定，它只是一个参数写错，而引用会指向别人的资料 —— 且看起来正常。
    """
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)
    env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)

    foreign = _chunks_for(document, [(0, 5)])[0]
    strayed = StoredChunk(
        chunk_id=foreign.chunk_id,
        tenant_id=foreign.tenant_id,
        project_id=_unique("proj_elsewhere"),
        source_id=foreign.source_id,
        document_id=foreign.document_id,
        chunk_index=0,
        heading_path=(),
        heading_level=0,
        span_start=0,
        span_end=5,
        content=foreign.content,
        parser_version=foreign.parser_version,
        created_at=foreign.created_at,
    )
    with pytest.raises(PlatformError) as excinfo:
        env.ingestion.complete(job, (strayed,))
    assert excinfo.value.code is ErrorCode.CROSS_PROJECT_DENIED

    # 被拒绝之后任务仍是 processing：可以修正后重来，而不是变成终态失败。
    assert (
        env.ingestion.get_job(_alice(), project_id, job.job_id).status
        is IngestionStatus.PROCESSING
    )


@pytest.mark.invariant
def test_chunk_index_gap_is_rejected(env):
    """序号必须是 0..n-1 连续。留空洞意味着有原文没有任何可引用片段，
    而检索结果完全看不出来。"""
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)
    env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)

    chunks = _chunks_for(document, [(0, 5), (5, 10)])
    shifted = (
        chunks[0],
        StoredChunk(
            chunk_id=chunks[1].chunk_id,
            tenant_id=chunks[1].tenant_id,
            project_id=chunks[1].project_id,
            source_id=chunks[1].source_id,
            document_id=chunks[1].document_id,
            chunk_index=7,  # 空洞
            heading_path=(),
            heading_level=0,
            span_start=chunks[1].span_start,
            span_end=chunks[1].span_end,
            content=chunks[1].content,
            parser_version=chunks[1].parser_version,
            created_at=chunks[1].created_at,
        ),
    )
    with pytest.raises(PlatformError) as excinfo:
        env.ingestion.complete(job, shifted)
    assert excinfo.value.code is ErrorCode.PARAMS_INVALID


@pytest.mark.invariant
def test_duplicate_delivery_produces_one_chunk_set_and_one_terminal_result(env):
    """重复投递是幂等成功：不追加第二套片段，也不报唯一键冲突。"""
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)
    claimed = env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None
    chunks = _chunks_for(document, [(0, 8), (8, 16)])

    env.ingestion.complete(claimed, chunks)
    env.ingestion.complete(claimed, chunks)  # 重放

    assert (
        env.ingestion.get_job(_alice(), project_id, job.job_id).status
        is IngestionStatus.SUCCEEDED
    )
    assert env.ingestion.stored_chunks(_alice(), project_id) == chunks


@pytest.mark.invariant
def test_complete_requires_processing_state(env):
    """没认领就完成会被拒绝：否则"任务状态"就不再是事实的记录，而是可跳过的装饰。"""
    project_id = _project(env)
    source = _register(env, project_id)
    document, job = _enqueue(env, project_id, source.source_id)

    with pytest.raises(PlatformError) as excinfo:
        env.ingestion.complete(job, _chunks_for(document, [(0, 5)]))
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.invariant
def test_fail_is_terminal_and_idempotent(env):
    project_id = _project(env)
    source = _register(env, project_id)
    _document, job = _enqueue(env, project_id, source.source_id)
    claimed = env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None

    env.ingestion.fail(claimed, error_code="PARSE_FAILED", safe_detail="原文不是有效的 UTF-8 文本")
    env.ingestion.fail(claimed, error_code="PARSE_FAILED", safe_detail="重复上报")

    stored = env.ingestion.get_job(_alice(), project_id, job.job_id)
    assert stored.status is IngestionStatus.FAILED
    assert stored.error_code == "PARSE_FAILED"
    assert stored.error_detail == "原文不是有效的 UTF-8 文本", "首次上报的详情不被重复上报覆盖"
    assert env.ingestion.claim_next(worker_id="worker-b", lease_seconds=300) is None


# ------------------------------------------------------------------ 隔离


@pytest.mark.invariant
def test_job_of_ungranted_principal_is_invisible(env):
    """同租户但未被授予该项目的主体看不到任务 —— 与"不存在"同一个拒绝。"""
    project_id = _project(env)
    source = _register(env, project_id)
    _document, job = _enqueue(env, project_id, source.source_id)

    with pytest.raises(PlatformError) as excinfo:
        env.ingestion.get_job(_bob(), project_id, job.job_id)
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED


@pytest.mark.invariant
def test_stored_chunks_are_scoped_to_the_project(env):
    """片段读取自行过滤：调用方不需要、也不应该自己判断作用域。"""
    project_a = _project(env)
    project_b = _project(env, actor=_bob())
    source_a = _register(env, project_a)
    document_a, job_a = _enqueue(env, project_a, source_a.source_id)
    claimed = env.ingestion.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None and claimed.job_id == job_a.job_id
    env.ingestion.complete(claimed, _chunks_for(document_a, [(0, 5)]))

    # 自己看得到。
    assert len(env.ingestion.stored_chunks(_alice(), project_a)) == 1
    # 别的项目看不到，也不会因为"同一个租户"而漏出来。
    assert env.ingestion.stored_chunks(_bob(), project_b) == ()


# ------------------------------------------------------------------ 不可变性


@pytest.mark.postgres
@pytest.mark.invariant
@pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_grants_match_the_contract_and_the_database():
    """权限的**三方一致**：迁移声明、生成契约、数据库实际授权。

    为什么直接查数据库而不是读迁移里的常量：迁移里的 `APPEND_ONLY` /
    `APP_ROLE_GRANTS_OVERRIDES` 都是人工声明，真正生效的是
    `information_schema.table_privileges`。0008 把 `ingestion_jobs` 的
    `UPDATE` 从应用角色收回并给了 worker 角色 —— 若契约生成器不认识这条声明，
    文档会继续写"应用角色能 UPDATE 队列"，而**没人会问为什么**（铁律 34）。

    （`http_idempotency` 曾经就是这个问题：策略只保护到租户级，
    而声明常量写着"租户级"，看起来完全正常。）
    """
    tables = ("source_documents", "ingestion_jobs", "source_chunks")
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        rows = conn.execute(
            "SELECT grantee, table_name, privilege_type FROM information_schema"
            ".table_privileges"
            " WHERE grantee IN ('study_app', 'study_worker') AND table_name = ANY(%s)",
            (list(tables),),
        ).fetchall()

    actual: dict[str, set[str]] = {}
    for grantee, table, privilege in rows:
        actual.setdefault(f"{grantee}:{table}", set()).add(privilege)

    # 数据库层的实际授权（R4-01 的全部要求都落在这一张表上）
    assert actual["study_app:source_documents"] == {"SELECT", "INSERT"}, actual
    assert actual["study_app:source_chunks"] == {"SELECT", "INSERT"}, actual
    # 应用角色不再能推进状态：`complete` / `fail` 是 worker 路径。
    assert actual["study_app:ingestion_jobs"] == {"SELECT", "INSERT"}, actual
    # worker 角色按最小集：认领要 SELECT + UPDATE（FOR UPDATE 需要行锁）。
    assert actual["study_worker:ingestion_jobs"] == {"SELECT", "UPDATE"}, actual
    assert actual["study_worker:source_documents"] == {"SELECT"}, actual
    assert actual["study_worker:source_chunks"] == {"SELECT", "INSERT"}, actual

    # 生成契约必须与数据库说同一件事。
    contract = _contract_role_grants()
    for table in tables:
        assert contract[table]["study_app"] == _as_contract_text(
            actual[f"study_app:{table}"]
        ), f"{table} 的应用角色权限：契约与数据库不一致"
        assert contract[table]["study_worker"] == _as_contract_text(
            actual[f"study_worker:{table}"]
        ), f"{table} 的 worker 角色权限：契约与数据库不一致"


#: 契约与迁移里的权限写法顺序。**不用字母序**：`SELECT, INSERT` 与
#: `INSERT, SELECT` 是同一个授权，但契约是给人读的，写法要稳定 ——
#: 两边各按自己的习惯排，比对就会变成"随机红"。
_PRIVILEGE_ORDER = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _as_contract_text(privileges: set[str]) -> str:
    """把数据库读回来的权限集排成契约里的写法。"""
    unknown = privileges - set(_PRIVILEGE_ORDER)
    assert not unknown, f"出现契约写法没覆盖的权限：{sorted(unknown)}"
    return ", ".join(name for name in _PRIVILEGE_ORDER if name in privileges)


def _contract_role_grants() -> dict[str, dict[str, str]]:
    """从生成的 `sql-schema.md` 表总览里读两类角色的权限列。"""
    path = (
        Path(__file__).resolve().parents[2] / "docs" / "skills" / "contracts" / "sql-schema.md"
    )
    text = path.read_text(encoding="utf-8")
    found: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6:
            continue
        name = cells[0].strip("`")
        if name not in {"source_documents", "ingestion_jobs", "source_chunks"}:
            continue
        found[name] = {"study_app": cells[3], "study_worker": cells[4]}
    assert set(found) == {"source_documents", "ingestion_jobs", "source_chunks"}, found
    return found
