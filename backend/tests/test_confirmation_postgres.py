"""确认记录 PostgreSQL 适配器的测试。

与内存版测试（`test_confirmation_and_auth.py`）覆盖同样的语义，
但运行在**真实数据库**上 —— 这一组测试验证的是：

- 单次消费的原子性由 `UPDATE ... WHERE consumed_at IS NULL` 保证，
  **跨连接、跨进程**成立（内存版只能靠进程内锁）；
- RLS 让「别的项目读这条确认」表现为不存在，而不是读到；
- 预算上界与绑定校验与内存版**完全一致**（共用 `validate_binding`）。

需要本地 PostgreSQL 运行（`scripts\\pg_start.cmd`），否则整组跳过。
数据值一律 ASCII：Git Bash 向 psql/数据库传中文会报 invalid byte sequence。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pg_support
import psycopg
import pytest
from app.budget.ledger import Dimension
from app.core.errors import ErrorCode, PlatformError
from app.db.confirmation_store import PostgresConfirmationStore
from app.execution.confirmation import BudgetCeiling

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
CURRENCY = str(Dimension.CURRENCY_MICROS)
TOOL = "append_project_evidence"
PARAMS = {"a": 1}


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not _postgres_reachable(),
        reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
    ),
]


def _seed(tenant_id: str, project_id: str) -> None:
    """种子数据用**超级用户**写入 —— 种子是运维动作，不该受应用角色 RLS 限制。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant_id, "seed tenant"),
            )
            conn.execute(
                "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)"
                " ON CONFLICT (project_id) DO NOTHING",
                (project_id, tenant_id, "seed project"),
            )


def _new_confirmation(ceiling_amount: int = 100):
    """每个测试用独立的租户/项目（uuid 后缀），互不干扰，也无需清理。"""
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    project_id = f"p_{uuid.uuid4().hex[:10]}"
    _seed(tenant_id, project_id)
    store = PostgresConfirmationStore()
    record = store.create(
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        budget_ceiling=BudgetCeiling(dimension=CURRENCY, amount=ceiling_amount),
        issued_at=NOW,
    )
    return store, record, tenant_id, project_id


# --------------------------------------------------------------- 基本语义


@pytest.mark.invariant
def test_created_record_is_readable_and_unconsumed():
    store, record, tenant_id, project_id = _new_confirmation()

    loaded = store.get(record.confirmation_id, tenant_id=tenant_id, project_id=project_id)

    assert loaded.consumed is False
    assert loaded.budget_ceiling.amount == 100
    assert loaded.budget_ceiling.dimension == CURRENCY


@pytest.mark.invariant
def test_peek_does_not_consume():
    store, record, tenant_id, project_id = _new_confirmation()

    store.peek(
        record.confirmation_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        now=NOW,
    )

    assert store.get(record.confirmation_id, tenant_id=tenant_id, project_id=project_id).consumed is False


@pytest.mark.invariant
def test_consume_marks_record_and_second_consume_fails():
    store, record, tenant_id, project_id = _new_confirmation()

    consumed = store.consume(
        record.confirmation_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        now=NOW,
    )
    assert consumed.consumed is True

    with pytest.raises(PlatformError) as exc:
        store.consume(
            record.confirmation_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id="u1",
            tool_id=TOOL,
            params=PARAMS,
            now=NOW,
        )
    assert exc.value.code is ErrorCode.POLICY_DENIED


# --------------------------------------------------------------- 原子性（跨连接）


@pytest.mark.invariant
def test_concurrent_consumption_admits_exactly_one():
    """跨连接的并发消费：恰好一个成功。

    这一次的原子性由数据库的 `UPDATE ... WHERE consumed_at IS NULL` 保证 ——
    与内存版的进程内锁不同，它跨进程、跨 worker 都成立。

    每个线程用**自己的连接**，这才是对数据库层原子性的真实验证；
    共享一条连接测出来的只是应用层时序。
    """
    store, record, tenant_id, project_id = _new_confirmation()
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            store.consume(
                record.confirmation_id,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id="u1",
                tool_id=TOOL,
                params=PARAMS,
                now=NOW,
            )
            result = "consumed"
        except PlatformError:
            result = "rejected"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("consumed") == 1, f"应当恰好成功一次，实际：{outcomes}"
    assert outcomes.count("rejected") == workers - 1


# --------------------------------------------------------------- 预算上界


@pytest.mark.invariant
def test_reservation_above_ceiling_is_rejected_without_consuming():
    store, record, tenant_id, project_id = _new_confirmation(ceiling_amount=100)

    with pytest.raises(PlatformError) as exc:
        store.consume(
            record.confirmation_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id="u1",
            tool_id=TOOL,
            params=PARAMS,
            now=NOW,
            reserved_budget={CURRENCY: 101},
        )

    assert exc.value.code is ErrorCode.BUDGET_EXCEEDED
    assert store.get(record.confirmation_id, tenant_id=tenant_id, project_id=project_id).consumed is False


@pytest.mark.invariant
def test_reservation_equal_to_ceiling_is_accepted():
    store, record, tenant_id, project_id = _new_confirmation(ceiling_amount=100)

    consumed = store.consume(
        record.confirmation_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        now=NOW,
        reserved_budget={CURRENCY: 100},
    )
    assert consumed.consumed is True


# --------------------------------------------------------------- RLS：跨项目


@pytest.mark.invariant
def test_confirmation_is_invisible_across_projects():
    """RLS：用别的项目上下文读确认 → 「不存在」，而不是读到。

    这验证的是「隔离收在数据库里」：应用层根本不需要记得过滤，
    传错项目就拿不到数据。
    """
    store, record, tenant_id, project_id = _new_confirmation()
    other_project = f"p_{uuid.uuid4().hex[:10]}"
    _seed(tenant_id, other_project)

    with pytest.raises(PlatformError):
        store.get(record.confirmation_id, tenant_id=tenant_id, project_id=other_project)


@pytest.mark.invariant
def test_cross_tenant_is_also_invisible():
    """跨租户同理 —— RLS 按 tenant 与 project 双重收窄。"""
    store, record, tenant_id, _project_id = _new_confirmation()
    other_tenant = f"t_{uuid.uuid4().hex[:10]}"
    other_project = f"p_{uuid.uuid4().hex[:10]}"
    _seed(other_tenant, other_project)

    with pytest.raises(PlatformError):
        store.get(record.confirmation_id, tenant_id=other_tenant, project_id=other_project)


# --------------------------------------------------------------- 绑定校验


@pytest.mark.invariant
def test_wrong_params_are_rejected_without_consuming():
    store, record, tenant_id, project_id = _new_confirmation()

    with pytest.raises(PlatformError):
        store.consume(
            record.confirmation_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id="u1",
            tool_id=TOOL,
            params={"a": 999},  # 与确认时固化的参数不同
            now=NOW,
        )

    assert store.get(record.confirmation_id, tenant_id=tenant_id, project_id=project_id).consumed is False


@pytest.mark.invariant
def test_wrong_principal_cannot_use_someone_confirmation():
    store, record, tenant_id, project_id = _new_confirmation()

    with pytest.raises(PlatformError) as exc:
        store.consume(
            record.confirmation_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id="someone_else",  # 确认不能转让
            tool_id=TOOL,
            params=PARAMS,
            now=NOW,
        )
    assert exc.value.code is ErrorCode.POLICY_DENIED
    assert store.get(record.confirmation_id, tenant_id=tenant_id, project_id=project_id).consumed is False
