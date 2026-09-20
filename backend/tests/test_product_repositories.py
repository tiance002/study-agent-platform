"""产品仓储的**契约测试**：内存与 PostgreSQL 适配器跑同一批断言。

延续 `test_identity_repositories.py` 的参数化模式。重点守三类
"不会报错、只会慢慢分叉"的语义：

1. **seq 分配**：并发追加不丢号不重号（PG 版是取号 + 插入同事务）；
2. **计划整版替换**：版本由实现分配、并发分配同版本按 VERSION_CONFLICT
   拒绝、替换的原子性（每版要么完整要么不存在）；
3. **资料去重**：同 identity_hash 重复登记返回既有记录（幂等成功，非报错）。

⚠️ PostgreSQL 参数被 skip 就等于这一关没过。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.product.memory_store import InMemoryProductRepository
from app.product.models import (
    LearningPlan,
    LearningTask,
    MessageRole,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

TENANT = "t_prod_pg"
OTHER_TENANT = "t_prod_pg_other"
ALICE = "u_prod_pg_alice"
BOB = "u_prod_pg_bob"
OUTSIDER = "u_prod_pg_out"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg_seed() -> None:
    """租户与主体的种子用超级用户写（运维动作）。库不可达时静默跳过：
    postgres 参数上的 skipif 负责跳过 PG 用例，内存用例不被牵连。"""
    if not _postgres_reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            for tenant in (TENANT, OTHER_TENANT):
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant, tenant),
                )
            for principal, tenant in ((ALICE, TENANT), (BOB, TENANT), (OUTSIDER, OTHER_TENANT)):
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                    " ON CONFLICT (principal_id) DO NOTHING",
                    (principal, tenant),
                )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _alice() -> Principal:
    return Principal(principal_id=ALICE, tenant_id=TENANT)


def _bob() -> Principal:
    return Principal(principal_id=BOB, tenant_id=TENANT)


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
def products(request, pg_seed):
    """返回 (产品仓储, membership)。"""
    if request.param == "memory":
        from app.identity.membership import MembershipStore

        membership = MembershipStore()
        return InMemoryProductRepository(membership=membership), membership

    from app.db.identity_store import PostgresMembershipRepository
    from app.db.product_store import PostgresProductRepository

    membership = PostgresMembershipRepository()
    return PostgresProductRepository(membership=membership), membership


def _bundle(goal: str = "三周入门", *, plan_id: str | None = None) -> PlanBundle:
    """构造一份成品 bundle（id 与 version 是占位值：
    replace 的契约是"版本由实现分配"，测试同样以返回值为准）。"""
    pid = plan_id or _unique("plan")
    m1 = _unique("mile")
    m2 = _unique("mile")
    return PlanBundle(
        plan=LearningPlan(
            plan_id=pid,
            tenant_id=TENANT,
            project_id="占位",
            version=99,
            goal=goal,
            status=PlanStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
        ),
        milestones=(
            Milestone(
                milestone_id=m1, tenant_id=TENANT, project_id="占位", plan_id=pid,
                order_index=0, title="第一周", description="",
            ),
            Milestone(
                milestone_id=m2, tenant_id=TENANT, project_id="占位", plan_id=pid,
                order_index=1, title="第二周", description="",
            ),
        ),
        tasks=(
            LearningTask(
                task_id=_unique("task"), tenant_id=TENANT, project_id="占位",
                milestone_id=m1, order_index=0, title="读第一章", status=TaskStatus.PENDING,
            ),
            LearningTask(
                task_id=_unique("task"), tenant_id=TENANT, project_id="占位",
                milestone_id=m2, order_index=0, title="写总结", status=TaskStatus.PENDING,
            ),
        ),
    )


# ------------------------------------------------------------ 会话与消息


@pytest.mark.invariant
def test_conversation_and_message_flow(products):
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="契约项目", goal="")

    conversation = products_repo.create_conversation(
        _alice(), project_id, conversation_id=_unique("conv"), title="问答"
    )
    assert conversation.last_message_seq == 0

    first = products_repo.append_message(
        _alice(), project_id, conversation.conversation_id,
        message_id=_unique("msg"), role=MessageRole.USER, content="第一问",
    )
    second = products_repo.append_message(
        _alice(), project_id, conversation.conversation_id,
        message_id=_unique("msg"), role=MessageRole.ASSISTANT, content="第一答",
    )
    assert (first.seq, second.seq) == (1, 2)

    listed = products_repo.list_messages(_alice(), project_id, conversation.conversation_id)
    assert [m.seq for m in listed] == [1, 2]


@pytest.mark.invariant
def test_ungranted_principal_cannot_touch_conversations(products):
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="甲的", goal="")
    conversation_id = products_repo.create_conversation(
        _alice(), project_id, conversation_id=_unique("conv"), title=""
    ).conversation_id

    with pytest.raises(PlatformError) as excinfo:
        products_repo.append_message(
            _bob(), project_id, conversation_id,
            message_id=_unique("msg"), role=MessageRole.USER, content="越权",
        )
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED
    # 越权追加不留下任何消息。
    assert products_repo.list_messages(_alice(), project_id, conversation_id) == ()


# ------------------------------------------------------------ 计划


@pytest.mark.invariant
def test_plan_replace_assigns_versions_and_returns_full_bundle(products):
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="", goal="")

    first = products_repo.replace_plan(_alice(), project_id, _bundle())
    assert first.plan.version == 1
    assert len(first.milestones) == 2 and len(first.tasks) == 2
    assert all(m.plan_id == first.plan.plan_id for m in first.milestones)

    second = products_repo.replace_plan(_alice(), project_id, _bundle(goal="第二版目标"))
    assert second.plan.version == 2, "版本由实现分配（入参 version 被忽略）"

    current = products_repo.current_plan(_alice(), project_id)
    assert current is not None and current.plan.version == 2
    assert current.plan.goal == "第二版目标"

    history = products_repo.plan_history(_alice(), project_id)
    assert [p.version for p in history] == [1, 2]


@pytest.mark.invariant
def test_no_plan_returns_none_not_empty_bundle(products):
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="", goal="")
    assert products_repo.current_plan(_alice(), project_id) is None


# ------------------------------------------------------------ 资料


@pytest.mark.invariant
def test_source_dedup_returns_existing_record(products):
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="", goal="")
    identity_hash = "sha256:" + uuid.uuid4().hex

    first = products_repo.register_source(
        _alice(), project_id,
        source_id=_unique("src"), display_name="讲义.pdf",
        media_type="application/pdf", identity_hash=identity_hash,
        acquisition={"kind": "upload"},
    )
    second = products_repo.register_source(
        _alice(), project_id,
        source_id=_unique("src"), display_name="讲义(重传).pdf",
        media_type="application/pdf", identity_hash=identity_hash,
        acquisition={"kind": "upload"},
    )
    assert second.source_id == first.source_id, "同资料重复登记返回既有记录（幂等成功）"
    assert len(products_repo.list_sources(_alice(), project_id)) == 1


# ------------------------------------------------------------ 并发取号


@pytest.mark.invariant
def test_concurrent_message_appends_have_no_gap_or_duplicate(products, racy_scheduling):
    """并发追加：seq 连续无重复。内存版靠锁、PG 版靠取号 UPDATE 的行锁，
    但"应该如此"要测过才算数（默认 GIL 间隔下无锁实现也能全过）。"""
    products_repo, membership = products
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="", goal="")
    conversation_id = products_repo.create_conversation(
        _alice(), project_id, conversation_id=_unique("conv"), title=""
    ).conversation_id

    attempts = 8
    barrier = threading.Barrier(attempts)
    results: list[object] = []
    lock = threading.Lock()

    def append() -> None:
        barrier.wait()
        try:
            message = products_repo.append_message(
                _alice(), project_id, conversation_id,
                message_id=_unique("msg"), role=MessageRole.USER, content="并发",
            )
            with lock:
                results.append(message.seq)
        except Exception as exc:  # noqa: BLE001 — 收集全部异常用于断言
            with lock:
                results.append(exc)

    threads = [threading.Thread(target=append) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(isinstance(r, int) for r in results), results
    assert sorted(results) == list(range(1, attempts + 1)), (
        f"seq 必须是 1..{attempts} 无空洞无重复，实际 {sorted(results)!r}"
    )
